"""Cited chat: rewrite -> retrieve -> gate -> assemble -> stream -> validate -> persist
(ARCHITECTURE §8, ADR-0022).

The service yields typed events and the API turns them into the SSE contract
(``meta``, ``sources``, ``token``, ``citation``, ``done``; ``error`` is the API's job).

Guarantees:
* Retrieval goes through SearchService/SearchIndex, so ACLs are enforced in SQL; the model
  only ever sees passages this user may read, and citations can only point at those.
* Retrieved text is data: it goes in the delimited user-turn block, never the system
  prompt, and model output triggers nothing.
* Every paid call is budget-checked first (core/domain/budget.py) and written to the
  usage ledger after. Chat is rate-limited per user, per minute and per day.
* Abstention: the gate before any model call, then the NO_ANSWER sentinel, which never
  reaches the user. Both persist the same designed "not enough evidence" reply.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

import structlog

from core.domain.answer import (
    DEFAULT_MIN_SIMILARITY,
    INSUFFICIENT_EVIDENCE,
    MAX_HISTORY_TURNS,
    MAX_QUESTION_CHARS,
    MAX_SOURCES,
    CitationTracker,
    Passage,
    SentinelFilter,
    Source,
    build_messages,
    number_sources,
    parse_rewrite,
    rewrite_messages,
    should_abstain_before_model,
    validate_citations,
)
from core.domain.auth import Identity
from core.domain.budget import SpendLimits
from core.domain.chat import ChatMessage, Role, Usage
from core.domain.data_class import strictest
from core.domain.documents import ScoredChunk, SearchFilters
from core.domain.errors import (
    BudgetExceeded,
    NotFound,
    RateLimited,
    UpstreamUnavailable,
    ValidationFailed,
)
from core.domain.ids import ChatSessionId, ChunkId, DocumentId, MessageId, WorkspaceId
from core.domain.search import MAX_QUERY_CHARS, FusedHit
from core.ports.chat import ChatStore, Citation, NewMessage
from core.ports.models import ChatModel, Tokenizer
from core.ports.rate_limit import Limit, RateLimiter
from core.ports.usage import UsageEntry, UsageLedger
from core.services.search import SearchService

log = structlog.get_logger(__name__)

CHAT_LIMIT = Limit(budget=6, window=timedelta(minutes=1))
CHAT_DAILY_LIMIT = Limit(budget=50, window=timedelta(days=1))
ANSWER_MAX_TOKENS = 700
REWRITE_MAX_TOKENS = 60
SOURCE_TOKEN_BUDGET = 3000  # passages plus their neighbours, in total
CLOSEST_SHOWN = 3
# USD per million tokens (input, output). Unknown models are priced high: fail safe.
CHAT_PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
    "gpt-4.1-mini": (Decimal("0.40"), Decimal("1.60")),
}
UNKNOWN_PRICE = (Decimal("5"), Decimal("15"))
# Pre-flight upper bound: system + 6 history turns + 3k of sources + question, and the
# rewrite. The exact request is re-checked once assembled.
ANSWER_ESTIMATE_TOKENS = (10_000, ANSWER_MAX_TOKENS + REWRITE_MAX_TOKENS)


# ---------------------------------------------------------------- events


@dataclass(frozen=True, slots=True)
class Meta:
    session_id: ChatSessionId
    user_message_id: MessageId
    query: str  # the standalone query actually searched (after rewriting)


@dataclass(frozen=True, slots=True)
class SourcesFound:
    sources: tuple[Source, ...]


@dataclass(frozen=True, slots=True)
class Token:
    text: str


@dataclass(frozen=True, slots=True)
class Cited:
    n: int
    chunk_id: ChunkId
    document_id: DocumentId


@dataclass(frozen=True, slots=True)
class Done:
    message_id: MessageId
    abstained: bool
    model: str | None
    latency_ms: int
    citations: tuple[int, ...] = ()
    closest: tuple[Source, ...] = field(default=())  # shown when abstaining
    notices: tuple[str, ...] = ()


AnswerEvent = Meta | SourcesFound | Token | Cited | Done


# ---------------------------------------------------------------- service


class AnswerService:
    def __init__(
        self,
        search: SearchService,
        model: ChatModel,
        chats: ChatStore,
        ledger: UsageLedger,
        limiter: RateLimiter,
        tokenizer: Tokenizer,
        *,
        limits: SpendLimits,
        min_similarity: float = DEFAULT_MIN_SIMILARITY,
    ) -> None:
        self._search = search
        self._model = model
        self._chats = chats
        self._ledger = ledger
        self._limiter = limiter
        self._tokenizer = tokenizer
        self._limits = limits
        self._min_similarity = min_similarity

    async def prepare(
        self, identity: Identity, question: str, session_id: ChatSessionId | None
    ) -> tuple[str, ChatSessionId | None]:
        """Every check that should fail as a plain HTTP error, before streaming starts:
        input, rate limits, budget, and that a given chat is this user's."""
        q = question.strip()
        if not q:
            raise ValidationFailed(
                "Type a question.", extra={"fields": {"question": "Type a question."}}
            )
        if len(q) > MAX_QUESTION_CHARS:
            message = f"Questions are limited to {MAX_QUESTION_CHARS:,} characters."
            raise ValidationFailed(message, extra={"fields": {"question": message}})
        if (
            session_id is not None
            and await self._chats.get_session(identity.workspace_id, identity.user_id, session_id)
            is None
        ):
            raise NotFound("This chat doesn't exist.")
        await self._spend_rate(identity)
        await self._check_budget(
            identity.workspace_id, _price(self._model.model, *ANSWER_ESTIMATE_TOKENS)
        )
        return q, session_id

    async def answer(
        self,
        identity: Identity,
        question: str,
        session_id: ChatSessionId | None,
        filters: SearchFilters | None = None,
    ) -> AsyncIterator[AnswerEvent]:
        """Call ``prepare`` first. Yields Meta, SourcesFound, Token/Cited..., Done."""
        started = time.perf_counter()
        ws, user = identity.workspace_id, identity.user_id
        sid = session_id or await self._chats.create_session(ws, user, _title(question))
        history = await self._history(identity, sid)
        user_msg = await self._chats.add_message(ws, user, sid, NewMessage(Role.USER, question))

        query = await self._rewrite(identity, question, history) if history else question
        yield Meta(sid, user_msg, query)

        results = await self._search.retrieve(
            identity, query[:MAX_QUERY_CHARS], filters or SearchFilters(), limit=MAX_SOURCES
        )
        passages = await self._assemble(identity, results.hits)
        sources = tuple(number_sources(passages))
        notices = results.notices

        if should_abstain_before_model(
            bool(passages), results.best_similarity, min_similarity=self._min_similarity
        ):
            log.info("answer.abstained", stage="gate", sources=len(sources))
            yield await self._abstain(
                identity, sid, user_msg, sources, started, notices, model=None
            )
            return

        yield SourcesFound(sources)
        messages = build_messages(question, sources, history)
        tokens_in = sum(self._tokenizer.count(m.content) for m in messages)
        # The real request size, not the pre-flight guess (security review).
        await self._check_budget(ws, _price(self._model.model, tokens_in, ANSWER_MAX_TOKENS))
        sentinel, tracker = SentinelFilter(), CitationTracker(s.n for s in sources)
        by_n = {s.n: s for s in sources}
        parts: list[str] = []
        usage: Usage | None = None
        data_class = strictest(p.data_class for p in passages)
        try:
            async for item in self._model.stream(messages, max_tokens=ANSWER_MAX_TOKENS):
                if isinstance(item, Usage):
                    usage = item
                    continue
                out = sentinel.feed(item.text)
                for ev in self._emit(out, parts, tracker, by_n):
                    yield ev
            for ev in self._emit(sentinel.flush(), parts, tracker, by_n):
                yield ev
        finally:
            # A stream cut by the client or the provider was still paid for: record the
            # real usage if it arrived, else an estimate (security review, ADR-0022).
            await self._record(ws, "chat", data_class, usage or self._estimate(tokens_in, parts))

        if sentinel.abstained:
            log.info("answer.abstained", stage="model", sources=len(sources))
            yield await self._abstain(
                identity, sid, user_msg, sources, started, notices, model=self._model.model
            )
            return

        text, kept = validate_citations("".join(parts), by_n)
        latency = _ms(started)
        msg_id = await self._chats.add_message(
            ws,
            user,
            sid,
            NewMessage(
                Role.ASSISTANT,
                text,
                parent_id=user_msg,
                model=self._model.model,
                usage=_usage_json(usage, self._model.model),
                latency_ms=latency,
            ),
        )
        await self._chats.add_citations(
            msg_id,
            [Citation(n, by_n[n].passage.chunk_id, by_n[n].passage.document_id) for n in kept],
        )
        log.info("answer.done", sources=len(sources), cited=len(kept), latency_ms=latency)
        yield Done(msg_id, False, self._model.model, latency, tuple(kept), notices=notices)

    async def feedback(
        self,
        identity: Identity,
        message: MessageId,
        *,
        rating: int,
        reason: str | None,
        comment: str | None,
    ) -> None:
        if rating not in (-1, 1):
            raise ValidationFailed("Rating must be 1 or -1.")
        await self._chats.save_feedback(
            identity.workspace_id,
            identity.user_id,
            message,
            rating=rating,
            reason=(reason or "").strip()[:40] or None,
            comment=(comment or "").strip()[:1000] or None,
        )

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _emit(
        out: str, parts: list[str], tracker: CitationTracker, by_n: dict[int, Source]
    ) -> list[AnswerEvent]:
        if not out:
            return []
        parts.append(out)
        events: list[AnswerEvent] = [Token(out)]
        for n in tracker.feed(out):
            p = by_n[n].passage
            events.append(Cited(n, p.chunk_id, p.document_id))
        return events

    async def _spend_rate(self, identity: Identity) -> None:
        if not await self._limiter.try_spend(f"chat:{identity.user_id}", 1, CHAT_LIMIT):
            raise RateLimited(
                "You're asking quickly. Wait a moment and try again.", retry_after_s=60
            )
        if not await self._limiter.try_spend(f"chat-day:{identity.user_id}", 1, CHAT_DAILY_LIMIT):
            raise RateLimited("You've reached today's question limit.", retry_after_s=86400)

    async def _check_budget(self, ws: WorkspaceId, upcoming: Decimal) -> None:
        spend = await self._ledger.spend(ws)
        if reason := self._limits.refusal(spend, upcoming):
            raise BudgetExceeded(reason)
        if self._limits.near_global_limit(spend):
            log.warning("budget_80_percent", spent_usd=str(spend.month_all))

    async def _history(self, identity: Identity, sid: ChatSessionId) -> list[ChatMessage]:
        stored = await self._chats.messages(
            identity.workspace_id, identity.user_id, sid, limit=MAX_HISTORY_TURNS
        )
        return [ChatMessage(m.role, m.content) for m in stored if m.role is not Role.SYSTEM]

    async def _rewrite(
        self, identity: Identity, question: str, history: Sequence[ChatMessage]
    ) -> str:
        """Standalone query for a follow-up. Any failure falls back to the raw question."""
        messages = rewrite_messages(question, history)
        tokens_in = sum(self._tokenizer.count(m.content) for m in messages)
        parts: list[str] = []
        usage: Usage | None = None
        try:
            async for item in self._model.stream(messages, max_tokens=REWRITE_MAX_TOKENS):
                if isinstance(item, Usage):
                    usage = item
                else:
                    parts.append(item.text)
            return parse_rewrite("".join(parts), question)
        except (UpstreamUnavailable, RateLimited) as exc:
            log.warning("answer.rewrite_failed", error=type(exc).__name__)
            return question
        finally:
            # The conversation may be about private content (fail closed, ADR-0012).
            await self._record(
                identity.workspace_id,
                "rewrite",
                "private",
                usage or self._estimate(tokens_in, parts),
            )

    async def _assemble(self, identity: Identity, hits: Sequence[FusedHit]) -> list[Passage]:
        """Top hits widened with neighbouring chunks (ordinal +-1) while under budget."""
        chunks: list[ScoredChunk] = [h.chunk for h in hits][:MAX_SOURCES]
        if not chunks:
            return []
        near = await self._search.neighbours(identity, [(c.document_id, c.ordinal) for c in chunks])
        by_pos = {(n.document_id, n.ordinal): n for n in near}
        budget = SOURCE_TOKEN_BUDGET - sum(self._tokenizer.count(c.content) for c in chunks)
        passages: list[Passage] = []
        for c in chunks:
            texts = [c.content]
            for pos, at_start in (
                ((c.document_id, c.ordinal - 1), True),
                ((c.document_id, c.ordinal + 1), False),
            ):
                n = by_pos.get(pos)
                if n is None:
                    continue
                cost = self._tokenizer.count(n.content)
                if cost > budget:
                    continue
                budget -= cost
                # A neighbour from another section keeps its own heading, or its facts
                # would be read under the hit's section (eval prod-road-001: a Q2 item
                # answered as "Q1 2027" because it sat in the Q1 passage unlabelled).
                text = n.content
                if n.heading_path and n.heading_path != c.heading_path:
                    text = f"[{n.heading_path}]\n{n.content}"
                texts.insert(0, text) if at_start else texts.append(text)
            passages.append(
                Passage(
                    chunk_id=c.chunk_id,
                    document_id=c.document_id,
                    title=c.title or "Untitled",
                    heading_path=c.heading_path,
                    content="\n\n".join(texts),
                    data_class=c.data_class,
                    ordinal=c.ordinal,
                )
            )
        return passages

    async def _abstain(
        self,
        identity: Identity,
        sid: ChatSessionId,
        user_msg: MessageId,
        sources: tuple[Source, ...],
        started: float,
        notices: tuple[str, ...],
        *,
        model: str | None,
    ) -> Done:
        latency = _ms(started)
        msg_id = await self._chats.add_message(
            identity.workspace_id,
            identity.user_id,
            sid,
            NewMessage(
                Role.ASSISTANT,
                INSUFFICIENT_EVIDENCE,
                parent_id=user_msg,
                model=model,
                usage={"abstained": True},
                latency_ms=latency,
            ),
        )
        return Done(msg_id, True, model, latency, closest=sources[:CLOSEST_SHOWN], notices=notices)

    def _estimate(self, tokens_in: int, parts: Sequence[str]) -> Usage:
        """Usage for a stream that ended without the provider's report."""
        out = self._tokenizer.count("".join(parts)) if parts else 0
        return Usage(model=self._model.model, tokens_in=tokens_in, tokens_out=out)

    async def _record(self, ws: WorkspaceId, kind: str, data_class: str, usage: Usage) -> None:
        await self._ledger.record(
            UsageEntry(
                workspace_id=ws,
                provider="openai",
                model=usage.model,
                kind=kind,
                data_class=data_class,
                tokens_in=usage.tokens_in,
                tokens_out=usage.tokens_out,
                cost_usd=_price(usage.model, usage.tokens_in, usage.tokens_out),
            )
        )


def _price(model: str, tokens_in: int, tokens_out: int) -> Decimal:
    price_in, price_out = CHAT_PRICES.get(model, UNKNOWN_PRICE)
    return (price_in * tokens_in + price_out * tokens_out) / Decimal(1_000_000)


def _usage_json(usage: Usage | None, model: str) -> dict[str, object] | None:
    if usage is None:
        return None
    cost = _price(model, usage.tokens_in, usage.tokens_out)
    return {"tokens_in": usage.tokens_in, "tokens_out": usage.tokens_out, "cost_usd": str(cost)}


def _title(question: str) -> str:
    one_line = " ".join(question.split())
    return one_line if len(one_line) <= 80 else one_line[:79].rstrip() + "…"


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
