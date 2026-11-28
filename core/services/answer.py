"""Cited chat: plan -> retrieve -> gate -> assemble -> stream -> validate -> persist
(ARCHITECTURE §8, ADR-0022, ADR-0031), with answer modes (ADR-0025).

The service yields typed events and the API turns them into the SSE contract
(``meta``, ``sources``, ``token``, ``citation``, ``done``; ``error`` is the API's job).

Guarantees:
* Retrieval goes through SearchService/SearchIndex, so ACLs are enforced in SQL; the model
  only ever sees passages this user may read, and citations can only point at those.
* Retrieved text is data: it goes in the delimited user-turn block, never the system
  prompt, and model output triggers nothing.
* Every paid call is budget-checked first (core/domain/budget.py) and written to the
  usage ledger after. Chat is rate-limited per user, per minute and per day.
* Planning (ADR-0031): every question except small talk gets one small planner call (its
  search queries, intent, period, source kinds and sender). The plan only narrows
  retrieval, the user's own filters win, and a plan whose filters find nothing is
  retried with the user's filters alone. A planner failure falls back to the question.
* Modes (ADR-0025), one answer call per question either way:
  - small talk (rules, ``is_small_talk``) skips retrieval and gets a short reply (``chat``);
  - when the gate finds nothing relevant, the model gets the question *without* sources
    and either answers from general knowledge (``general``) or replies NO_ANSWER;
  - otherwise the grounded prompt, whose reply is cited, general (GENERAL_ANSWER) or
    NO_ANSWER.
  NO_ANSWER never reaches the user: it persists the same designed "not enough evidence"
  reply. General and chat calls see no sources, so they can't leak any.
* Vague questions (ADR-0043), at most two deterministic steps (no model tool calls):
  - freshness: a question about the last hours first syncs its sources if they're stale
    (short, cooperative, throttled), or says when they last synced;
  - expansion: when the question wants what something contains, or the top hits carry
    files, unread files are read on demand (deadline-bound; the worker finishes the
    rest) and their text, plus more of the top document, joins the sources. Everything
    is fetched through the same ACL-filtered SQL as search.
  - every degradation becomes a notice (stale source, unreadable file, relaxed filters),
    shown with the answer or the abstention.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal
from typing import Protocol

import structlog

from core.domain.answer import (
    CHAT_PROMPT,
    DEFAULT_MIN_SIMILARITY,
    GENERAL_PROMPT,
    INSUFFICIENT_EVIDENCE,
    MAX_HISTORY_TURNS,
    MAX_QUESTION_CHARS,
    MAX_SOURCES,
    AnswerMode,
    CitationTracker,
    FollowupFilter,
    Passage,
    SentinelFilter,
    Source,
    build_messages,
    is_small_talk,
    number_sources,
    plain_messages,
    should_abstain_before_model,
    validate_citations,
)
from core.domain.attachments import (
    INDEXED,
    RETRYABLE,
    AttachmentInfo,
    AttachmentState,
    explain,
)
from core.domain.auth import Identity
from core.domain.auth import Role as AccountRole
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
from core.domain.planner import (
    CONNECTORS_BY_KIND,
    QueryPlan,
    fallback_plan,
    local_zone,
    merge_filters,
    parse_plan,
    planner_messages,
    search_queries,
)
from core.domain.rerank import RERANK_CANDIDATES, RERANK_KEEP, RERANK_MAX_TOKENS, snippet
from core.domain.search import DIVERSITY_POOL, FusedHit, diversify
from core.ports.chat import ChatStore, Citation, NewMessage
from core.ports.jobs import WorkerTrigger
from core.ports.models import ChatModel, Tokenizer
from core.ports.rate_limit import Limit, RateLimiter
from core.ports.reranker import RerankCandidate, Reranker
from core.ports.system import Clock
from core.ports.usage import UsageEntry, UsageLedger
from core.services.attachments import AttachmentJobs, AttachmentReader
from core.services.search import SearchResults, SearchService

log = structlog.get_logger(__name__)

CHAT_LIMIT = Limit(budget=6, window=timedelta(minutes=1))
CHAT_DAILY_LIMIT = Limit(budget=50, window=timedelta(days=1))
ANSWER_MAX_TOKENS = 900  # room for a dated summary of several items
CHAT_MAX_TOKENS = 200  # small talk is short
PLAN_MAX_TOKENS = 200  # one small JSON object
# Passages plus their neighbours, in total (ADR-0031; was 3,000, which 8 chunks of ~512
# tokens already filled, so no neighbour ever fitted). Hits get at most HIT_TOKEN_BUDGET;
# the rest widens the best hits with their neighbours.
SOURCE_TOKEN_BUDGET = 8000
HIT_TOKEN_BUDGET = 6000
CLOSEST_SHOWN = 3
# Expansion (ADR-0043): files of the top hits are read and added to the sources.
EXPAND_TOP = 3  # hits whose documents may be expanded
EXPAND_CHUNKS = 8  # attachment / whole-document chunks fetched
EXPAND_TOKEN_BUDGET = 5000  # on top of SOURCE_TOKEN_BUDGET
READ_BUDGET_S = 20.0  # on-demand file reads a question may wait for
EMAIL_CONNECTORS = CONNECTORS_BY_KIND["email"]
# USD per million tokens (input, output). Unknown models are priced high: fail safe.
CHAT_PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
    "gpt-4.1-mini": (Decimal("0.40"), Decimal("1.60")),
}
UNKNOWN_PRICE = (Decimal("5"), Decimal("15"))
# Pre-flight upper bound: system + 6 history turns + 8k of sources + question, and the
# planner. The exact request is re-checked once assembled.
ANSWER_ESTIMATE_TOKENS = (16_000, ANSWER_MAX_TOKENS + PLAN_MAX_TOKENS)


# ---------------------------------------------------------------- events


@dataclass(frozen=True, slots=True)
class Meta:
    session_id: ChatSessionId
    user_message_id: MessageId
    query: str  # the standalone query actually searched (after rewriting)
    # The planner's decision (ADR-0031), for evals and diagnosis; None for small talk.
    plan: QueryPlan | None = None


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
    mode: AnswerMode = "grounded"
    followups: tuple[str, ...] = ()  # suggested next questions, grounded answers only (P2-1)


AnswerEvent = Meta | SourcesFound | Token | Cited | Done


class Freshness(Protocol):
    """Syncs a question's stale sources (``SourceActions.freshen``)."""

    async def freshen(
        self, identity: Identity, connectors: Sequence[str], *, now: datetime, zone: tzinfo
    ) -> tuple[str, ...]: ...


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
        clock: Clock | None = None,
        reranker: Reranker | None = None,
        freshness: Freshness | None = None,
        attachments: AttachmentReader | None = None,
        attachment_jobs: AttachmentJobs | None = None,
        trigger: WorkerTrigger | None = None,
    ) -> None:
        # ADR-0043: each is optional; without one, that step is skipped (never an error).
        self._freshness = freshness
        self._attachments = attachments
        self._attachment_jobs = attachment_jobs
        self._trigger = trigger
        # The optional selection step (P1-4, ADR-0040); None: fused rank order.
        self._reranker = reranker
        self._search = search
        self._model = model
        self._chats = chats
        self._ledger = ledger
        self._limiter = limiter
        self._tokenizer = tokenizer
        self._limits = limits
        self._min_similarity = min_similarity
        self._clock = clock

    async def prepare(
        self,
        identity: Identity,
        question: str,
        session_id: ChatSessionId | None,
        replace_from: MessageId | None = None,
    ) -> tuple[str, ChatSessionId | None]:
        """Every check that should fail as a plain HTTP error, before streaming starts:
        input, rate limits, budget, that a given chat is this user's, and that a turn being
        regenerated or edited (``replace_from``, ADR-0024) is a live question in it."""
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
        if replace_from is not None:
            if session_id is None:
                raise ValidationFailed("Regenerating or editing needs the chat it belongs to.")
            live = await self._chats.messages(
                identity.workspace_id, identity.user_id, session_id, limit=200
            )
            if not any(m.id == replace_from and m.role is Role.USER for m in live):
                raise NotFound("This message doesn't exist.")
        await self._spend_rate(identity)
        await self._check_budget(identity, _price(self._model.model, *ANSWER_ESTIMATE_TOKENS))
        return q, session_id

    async def answer(
        self,
        identity: Identity,
        question: str,
        session_id: ChatSessionId | None,
        filters: SearchFilters | None = None,
        *,
        replace_from: MessageId | None = None,
        timezone: str | None = None,
    ) -> AsyncIterator[AnswerEvent]:
        """Call ``prepare`` first. Yields Meta, SourcesFound, Token/Cited..., Done.

        ``replace_from`` regenerates or edits: that question and everything after it are
        superseded in the same write that stores the new question, *before* history is
        read, so the replaced turns never reach the prompt (ADR-0024). ``timezone`` is the
        browser's IANA zone: it decides what "today" and "last week" mean (UTC if unknown).
        """
        started = time.perf_counter()
        ws, user = identity.workspace_id, identity.user_id
        zone = local_zone(timezone)
        now = self._now().astimezone(zone)
        sid = session_id or await self._chats.create_session(ws, user, _title(question))
        user_msg = await self._chats.add_message(
            ws, user, sid, NewMessage(Role.USER, question), supersede_from=replace_from
        )
        history, anchor = await self._history(identity, sid, exclude=user_msg)

        if is_small_talk(question):
            yield Meta(sid, user_msg, question)
            log.info("answer.routed", mode="chat")
            async for ev in self._unsourced(
                identity, sid, user_msg, question, history, started, now=now
            ):
                yield ev
            return

        plan = await self._plan(identity, question, history, now)
        queries = search_queries(plan, question, first_turn=not history)
        yield Meta(sid, user_msg, queries[0], plan)

        fresh = await self._freshen(identity, plan, now, zone)
        user_filters = filters or SearchFilters()
        results, matched = await self._retrieve(
            identity, queries, user_filters, plan, zone, anchor=anchor, now=now
        )
        # Without a source filter, every connector that found something strong gets a
        # voice (P1-7); with one, the person asked for that source only.
        narrowed = bool(plan.kinds or user_filters.connectors or user_filters.source_ids)
        hits = (
            list(results.hits[:MAX_SOURCES]) if narrowed else diversify(results.hits, MAX_SOURCES)
        )
        if self._reranker is not None and plan.intent == "lookup":
            # Recent and summary questions are about dates, not relevance: no selection.
            hits = await self._select(identity, queries[0], results.hits) or hits
        passages = await self._assemble(identity, hits)
        extra, file_notes = await self._expand(identity, plan, hits, queries[0], now)
        passages = [*passages, *extra]
        sources = tuple(number_sources(passages))
        notices = tuple(dict.fromkeys((*fresh, *results.notices, *file_notes)))

        # "What's new?" resembles no passage, so the similarity gate would always fire;
        # with the recent lane, any document found is the evidence (ADR-0031).
        # The plan's own filters (sent, a person, the last hours) matching is evidence too:
        # an email whose only text is "Test" resembles no question (ADR-0043).
        best = None if plan.wants_recent or matched else results.best_similarity
        if should_abstain_before_model(bool(passages), best, min_similarity=self._min_similarity):
            # Nothing relevant: the model may still answer a general question, without
            # sources; a question about the user's own data still abstains (ADR-0025).
            log.info("answer.routed", mode="general", sources=len(sources))
            async for ev in self._unsourced(
                identity, sid, user_msg, question, history, started, closest=sources,
                notices=notices, now=now,
            ):  # fmt: skip
                yield ev
            return

        yield SourcesFound(sources)
        messages = build_messages(question, sources, history, now=now)
        tokens_in = sum(self._tokenizer.count(m.content) for m in messages)
        # The real request size, not the pre-flight guess (security review).
        await self._check_budget(identity, _price(self._model.model, tokens_in, ANSWER_MAX_TOKENS))
        sentinel, tracker = SentinelFilter(), CitationTracker(s.n for s in sources)
        follow = FollowupFilter()  # strips the hidden FOLLOWUPS line (P2-1)
        no_cites = CitationTracker(())
        by_n = {s.n: s for s in sources}
        parts: list[str] = []
        usage: Usage | None = None
        data_class = strictest(p.data_class for p in passages)
        try:
            async for item in self._model.stream(messages, max_tokens=ANSWER_MAX_TOKENS):
                if isinstance(item, Usage):
                    usage = item
                    continue
                out = follow.feed(sentinel.feed(item.text))
                # A general answer (GENERAL_ANSWER) confirms no citations.
                cites = no_cites if sentinel.general else tracker
                for ev in self._emit(out, parts, cites, by_n):
                    yield ev
            cites = no_cites if sentinel.general else tracker
            tail = follow.feed(sentinel.flush()) + follow.flush()
            for ev in self._emit(tail, parts, cites, by_n):
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

        mode = sentinel.mode
        # A general answer cites nothing: any [n] it wrote is dropped (rule 9).
        text, kept = validate_citations("".join(parts), by_n if mode == "grounded" else {})
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
                usage={
                    **_usage_json(usage, self._model.model, mode),
                    "followups": list(follow.followups) if mode == "grounded" else [],
                },
                latency_ms=latency,
            ),
        )
        await self._chats.add_citations(
            msg_id,
            [Citation(n, by_n[n].passage.chunk_id, by_n[n].passage.document_id) for n in kept],
        )
        log.info(
            "answer.done", mode=mode, sources=len(sources), cited=len(kept), latency_ms=latency
        )
        yield Done(
            msg_id, False, self._model.model, latency, tuple(kept), notices=notices, mode=mode,
            followups=follow.followups if mode == "grounded" else (),
        )  # fmt: skip

    async def _unsourced(
        self,
        identity: Identity,
        sid: ChatSessionId,
        user_msg: MessageId,
        question: str,
        history: Sequence[ChatMessage],
        started: float,
        *,
        closest: tuple[Source, ...] | None = None,
        notices: tuple[str, ...] = (),
        now: datetime | None = None,
    ) -> AsyncIterator[AnswerEvent]:
        """One model call without sources: small talk (``closest is None``) or the general
        fallback, which may still abstain for a question about the user's own data."""
        ws, user = identity.workspace_id, identity.user_id
        small_talk = closest is None
        messages = plain_messages(
            CHAT_PROMPT if small_talk else GENERAL_PROMPT, question, history, now=now
        )
        tokens_in = sum(self._tokenizer.count(m.content) for m in messages)
        max_tokens = CHAT_MAX_TOKENS if small_talk else ANSWER_MAX_TOKENS
        await self._check_budget(identity, _price(self._model.model, tokens_in, max_tokens))
        sentinel = SentinelFilter()
        parts: list[str] = []
        usage: Usage | None = None
        try:
            async for item in self._model.stream(messages, max_tokens=max_tokens):
                if isinstance(item, Usage):
                    usage = item
                    continue
                out = sentinel.feed(item.text)
                if out:
                    parts.append(out)
                    yield Token(out)
            if out := sentinel.flush():
                parts.append(out)
                yield Token(out)
        finally:
            # History may hold private content, so the call is classed private (ADR-0012).
            await self._record(ws, "chat", "private", usage or self._estimate(tokens_in, parts))

        if sentinel.abstained or not "".join(parts).strip():
            log.info("answer.abstained", stage="general")
            yield await self._abstain(
                identity, sid, user_msg, closest or (), started, notices, model=self._model.model
            )
            return
        mode: AnswerMode = "chat" if small_talk else "general"
        text, _ = validate_citations("".join(parts), {})  # no sources: no citations
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
                usage=_usage_json(usage, self._model.model, mode),
                latency_ms=latency,
            ),
        )
        log.info("answer.done", mode=mode, latency_ms=latency)
        yield Done(msg_id, False, self._model.model, latency, notices=notices, mode=mode)

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

    async def _check_budget(self, identity: Identity, upcoming: Decimal) -> None:
        spend = await self._ledger.spend(identity.workspace_id)
        if reason := self._limits.refusal(spend, upcoming, privileged=_owner_at_home(identity)):
            raise BudgetExceeded(reason)
        if self._limits.near_global_limit(spend):
            log.warning("budget_80_percent", spent_usd=str(spend.month_all))

    async def _history(
        self, identity: Identity, sid: ChatSessionId, *, exclude: MessageId
    ) -> tuple[list[ChatMessage], tuple[DocumentId, ...]]:
        """The turns before the question just stored (``exclude``), and the documents the
        latest answer cited: the anchor for a follow-up (P1-5). Ids only; the search lane
        re-checks access to each before anything is shown."""
        stored = [
            m
            for m in await self._chats.messages(
                identity.workspace_id, identity.user_id, sid, limit=MAX_HISTORY_TURNS + 1
            )
            if m.id != exclude and m.role is not Role.SYSTEM
        ]
        history = [ChatMessage(m.role, m.content) for m in stored][-MAX_HISTORY_TURNS:]
        last_answer = next((m for m in reversed(stored) if m.role is Role.ASSISTANT), None)
        cited = (
            ()
            if last_answer is None
            else tuple(dict.fromkeys(c.document_id for c in last_answer.citations if c.document_id))
        )
        return history, cited

    def _now(self) -> datetime:
        return self._clock.now() if self._clock is not None else datetime.now(UTC)

    async def _plan(
        self,
        identity: Identity,
        question: str,
        history: Sequence[ChatMessage],
        now: datetime,
    ) -> QueryPlan:
        """The search plan for this question (ADR-0031). Any failure falls back to the
        question alone: a failed planner degrades the answer, never fails it."""
        messages = planner_messages(question, history, now)
        tokens_in = sum(self._tokenizer.count(m.content) for m in messages)
        parts: list[str] = []
        usage: Usage | None = None
        try:
            async for item in self._model.stream(messages, max_tokens=PLAN_MAX_TOKENS):
                if isinstance(item, Usage):
                    usage = item
                else:
                    parts.append(item.text)
            return parse_plan("".join(parts), question, today=now.date(), history=history)
        except (UpstreamUnavailable, RateLimited) as exc:
            log.warning("answer.plan_failed", error=type(exc).__name__)
            return fallback_plan(question)
        finally:
            # The conversation may be about private content (fail closed, ADR-0012).
            await self._record(
                identity.workspace_id,
                "rewrite",
                "private",
                usage or self._estimate(tokens_in, parts),
            )

    async def _retrieve(
        self,
        identity: Identity,
        queries: Sequence[str],
        user_filters: SearchFilters,
        plan: QueryPlan,
        zone: tzinfo,
        *,
        anchor: Sequence[DocumentId] = (),
        now: datetime | None = None,
    ) -> tuple[SearchResults, bool]:
        """Retrieval under the plan's filters merged with the user's. If the plan's own
        period, kind or sender hid everything, retry with the user's filters alone: a
        guess about the question must not turn into "nothing found" (ADR-0031). The
        answer is told (a notice), so it doesn't present the closest results as a match.

        Also returns whether the plan's own conversation filters (direction, person, hours)
        matched directly: then the hits are what was asked for, whatever their similarity."""
        results = await self._search.retrieve(
            identity,
            queries,
            merge_filters(user_filters, plan, zone, now=now),
            limit=DIVERSITY_POOL,  # a pool: _answer keeps MAX_SOURCES (P1-7)
            recent=plan.wants_recent,
            anchor=anchor,
        )
        matched = bool(results.hits) and bool(plan.direction or plan.person or plan.since_hours)
        if not results.hits and plan.narrows:
            log.info("answer.plan_filters_dropped")
            results = await self._search.retrieve(
                identity, queries, user_filters, limit=DIVERSITY_POOL, recent=plan.wants_recent,
                anchor=anchor,
            )  # fmt: skip
            if results.hits:
                results = replace(results, notices=(*results.notices, relaxed_notice(plan)))
        log.info(
            "answer.planned",
            planned=plan.planned,
            intent=plan.intent,
            queries=len(queries),
            narrowed=plan.narrows,
            hits=len(results.hits),
        )  # never the queries themselves: they're private text
        return results, matched

    async def _select(
        self, identity: Identity, question: str, pool: Sequence[FusedHit]
    ) -> list[FusedHit] | None:
        """The LLM selection step (P1-4): which of the top fused hits are worth reading.
        None (use the rank order) when the pool is small, the budget can't cover it, the
        provider fails, or the reply is unusable. A failed selection never fails an answer."""
        assert self._reranker is not None
        offered = list(pool[:RERANK_CANDIDATES])
        if len(offered) <= RERANK_KEEP:
            return None
        candidates = [
            RerankCandidate(
                n=i,
                title=h.chunk.title or "Untitled",
                kind=h.chunk.connector,
                date=h.chunk.updated_at.date().isoformat() if h.chunk.updated_at else None,
                snippet=snippet(h.chunk.content),
            )
            for i, h in enumerate(offered, start=1)
        ]
        tokens_in = 200 + sum(self._tokenizer.count(c.snippet) + 20 for c in candidates)
        model = self._reranker.model
        try:
            await self._check_budget(identity, _price(model, tokens_in, RERANK_MAX_TOKENS))
            selection = await self._reranker.select(question, candidates, RERANK_KEEP)
        except (BudgetExceeded, UpstreamUnavailable, RateLimited) as exc:
            log.info("answer.rerank_skipped", reason=type(exc).__name__)
            return None
        # Passage text is private data (fail closed, ADR-0012).
        await self._record(
            identity.workspace_id, "rerank", "private",
            selection.usage or Usage(model=model, tokens_in=tokens_in, tokens_out=0),
        )  # fmt: skip
        if not selection.keep:
            log.info("answer.rerank_skipped", reason="unusable reply")
            return None
        log.info("answer.reranked", offered=len(offered), kept=len(selection.keep))
        return [offered[n - 1] for n in selection.keep]

    async def _assemble(self, identity: Identity, hits: Sequence[FusedHit]) -> list[Passage]:
        """Top hits, then their neighbouring chunks (ordinal +-1), under the token budget.

        Hits are kept in rank order while they fit ``HIT_TOKEN_BUDGET``; the rest of
        ``SOURCE_TOKEN_BUDGET`` widens the best hits first. A chunk already shown, as a
        hit or a neighbour, is never repeated, so adjacent hits don't duplicate text.
        """
        chunks = self._fit_hits([h.chunk for h in hits][:MAX_SOURCES])
        if not chunks:
            return []
        near = await self._search.neighbours(identity, [(c.document_id, c.ordinal) for c in chunks])
        by_pos = {(n.document_id, n.ordinal): n for n in near}
        shown = {(c.document_id, c.ordinal) for c in chunks}
        budget = SOURCE_TOKEN_BUDGET - sum(self._tokenizer.count(c.content) for c in chunks)
        passages: list[Passage] = []
        for c in chunks:
            texts = [c.content]
            for pos, at_start in (
                ((c.document_id, c.ordinal - 1), True),
                ((c.document_id, c.ordinal + 1), False),
            ):
                n = by_pos.get(pos)
                if n is None or pos in shown:
                    continue
                cost = self._tokenizer.count(n.content)
                if cost > budget:
                    continue
                budget -= cost
                shown.add(pos)
                # A neighbour from another section keeps its own heading, or its facts
                # would be read under the hit's section (eval prod-road-001: a Q2 item
                # answered as "Q1 2027" because it sat in the Q1 passage unlabelled).
                text = n.content
                if n.heading_path and n.heading_path != c.heading_path:
                    text = f"[{n.heading_path}]\n{n.content}"
                texts.insert(0, text) if at_start else texts.append(text)
            if c.attachments:  # what each file is, and why one can't be read (ADR-0043)
                texts.append(files_line(c.attachments))
            passages.append(_passage(c, "\n\n".join(texts)))
        return passages

    async def _freshen(
        self, identity: Identity, plan: QueryPlan, now: datetime, zone: tzinfo
    ) -> tuple[str, ...]:
        """Sync the sources a question about the last hours depends on (ADR-0043)."""
        if self._freshness is None or not touches_last_hours(plan, now):
            return ()
        connectors = freshness_connectors(plan)
        if not connectors:
            return ()
        try:
            return await self._freshness.freshen(identity, connectors, now=now, zone=zone)
        except Exception as e:  # freshness only improves an answer; it never fails one
            log.warning("answer.freshen_failed", error=type(e).__name__)
            return ()

    async def _expand(
        self,
        identity: Identity,
        plan: QueryPlan,
        hits: Sequence[FusedHit],
        query: str,
        now: datetime,
    ) -> tuple[list[Passage], tuple[str, ...]]:
        """Step two (ADR-0043): the top hits' files, and more of the top document when the
        question wants its contents. Returns extra passages and notices."""
        top = [h.chunk for h in hits[:EXPAND_TOP]]
        if not top:
            return [], ()
        parents: dict[DocumentId, ScoredChunk] = {}
        for c in top:
            parents.setdefault(c.parent_id or c.document_id, c)
        with_files = [
            pid for pid, c in parents.items()
            if c.parent_id is not None or c.attachments or mentions_files(c.content)
        ]  # fmt: skip
        if not with_files and not plan.wants_content:
            return [], ()
        ws = identity.workspace_id
        notes: list[str] = []
        pending: list[DocumentId] = []
        deadline = time.monotonic() + READ_BUDGET_S
        if self._attachments is not None:
            for pid in with_files:
                c = parents[pid]
                if c.parent_id is not None or not self._attachments.can_read(c.connector):
                    continue
                if c.attachments and not any(a.state in RETRYABLE for a in c.attachments):
                    continue  # nothing left to read
                out = await self._attachments.read(ws, identity.principals, pid, deadline=deadline)
                if out.pending:
                    pending.append(pid)
                notes += [explain(a) for a in out.attachments if a.state not in INDEXED | RETRYABLE]
                waiting = [a.name for a in out.attachments if a.state is AttachmentState.LISTED]
                if out.pending and waiting:
                    notes.append(reading_notice(waiting))
        kids = await self._search.attachment_children(identity, list(parents))
        targets = [child for child, _ in kids]
        if plan.wants_content:
            targets.append(top[0].parent_id or top[0].document_id)  # read it whole
        shown = {h.chunk.chunk_id for h in hits[:MAX_SOURCES]}
        chunks = [
            c for c in await self._search.document_chunks(identity, targets, query, EXPAND_CHUNKS)
            if c.chunk_id not in shown
        ]  # fmt: skip
        extra: list[Passage] = []
        budget = EXPAND_TOKEN_BUDGET
        for c in chunks:
            cost = self._tokenizer.count(c.content)
            if cost > budget:
                continue
            budget -= cost
            extra.append(_passage(c, c.content))
        await self._queue_attachment_work(ws, pending, [p for _, p in kids], now)
        log.info(
            "answer.expanded", parents=len(with_files), children=len(kids), added=len(extra),
            pending=len(pending),
        )  # fmt: skip
        return extra, tuple(notes)

    async def _queue_attachment_work(
        self,
        ws: WorkspaceId,
        pending: Sequence[DocumentId],
        used: Sequence[DocumentId],
        now: datetime,
    ) -> None:
        """Unfinished reads go to the worker now; attachments an answer used get their
        vectors (Tier 2) at the worker's next run. Best effort: never fails an answer."""
        if self._attachment_jobs is None or not (pending or used):
            return
        try:
            hour = int(now.timestamp()) // 3600
            await self._attachment_jobs.enqueue(ws, [*pending, *used], hour=hour)
            if pending and self._trigger is not None:
                await self._trigger.trigger("attachments")
        except Exception as e:
            log.warning("answer.attachment_queue_failed", error=type(e).__name__)

    def _fit_hits(self, chunks: Sequence[ScoredChunk]) -> list[ScoredChunk]:
        """Hits in rank order while they fit HIT_TOKEN_BUDGET (the best one always)."""
        kept: list[ScoredChunk] = []
        used = 0
        for c in chunks:
            cost = self._tokenizer.count(c.content)
            if kept and used + cost > HIT_TOKEN_BUDGET:
                continue
            kept.append(c)
            used += cost
        return kept

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


def _usage_json(usage: Usage | None, model: str, mode: AnswerMode) -> dict[str, object]:
    if usage is None:
        return {"mode": mode}
    cost = _price(model, usage.tokens_in, usage.tokens_out)
    return {
        "tokens_in": usage.tokens_in,
        "tokens_out": usage.tokens_out,
        "cost_usd": str(cost),
        "mode": mode,
    }


def _passage(c: ScoredChunk, content: str) -> Passage:
    return Passage(
        chunk_id=c.chunk_id,
        document_id=c.document_id,
        title=c.title or "Untitled",
        heading_path=c.heading_path,
        content=content,
        data_class=c.data_class,
        ordinal=c.ordinal,
        connector=c.connector,
        updated_at=c.updated_at,
        author=c.author,
    )


def _title(question: str) -> str:
    one_line = " ".join(question.split())
    return one_line if len(one_line) <= 80 else one_line[:79].rstrip() + "…"


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _owner_at_home(identity: Identity) -> bool:
    """The platform owner, acting in a workspace they own (ADR-0029/0030): never in someone
    else's workspace, and never for anyone they invited."""
    return identity.is_platform_admin and identity.role is AccountRole.OWNER


def touches_last_hours(plan: QueryPlan, now: datetime) -> bool:
    """The question is about the last few hours: "today", "just now", "this morning"."""
    if plan.since_hours is not None:
        return True
    today = now.date()
    return (
        plan.after is not None
        and plan.after >= today - timedelta(days=1)
        and (plan.before is None or plan.before >= today)
    )


def freshness_connectors(plan: QueryPlan) -> tuple[str, ...]:
    """Which sources to check: the kinds the question names, or email when it talks about
    sending or receiving, or about someone (vague mail questions)."""
    if plan.kinds:
        return tuple(c for k in plan.kinds for c in CONNECTORS_BY_KIND[k])
    if plan.direction or plan.person:
        return EMAIL_CONNECTORS
    return ()


def mentions_files(content: str) -> bool:
    """Text indexed before attachments were listed still names them (ADR-0043)."""
    return "[attachment:" in content


def files_line(files: Sequence[AttachmentInfo]) -> str:
    """One line per document: each file and what Slate knows of it."""
    parts: list[str] = []
    for f in files[:10]:
        if f.state in INDEXED:
            parts.append(f"{f.name} (contents indexed{', first part only' if f.truncated else ''})")
        elif f.state in RETRYABLE:
            parts.append(f"{f.name} (not read yet)")
        else:
            parts.append(explain(f))
    more = f" and {len(files) - 10} more" if len(files) > 10 else ""
    return "[Files: " + "; ".join(parts) + more + "]"


def reading_notice(names: Sequence[str]) -> str:
    shown = ", ".join(names[:3]) + (" and more" if len(names) > 3 else "")
    return f"Still reading {shown}. Ask again in a minute to include what it says."


def relaxed_notice(plan: QueryPlan) -> str:
    """Said when the plan's own filters found nothing and the search was widened."""
    what: list[str] = []
    if plan.direction == "sent":
        what.append("mail you sent")
    elif plan.direction == "received":
        what.append("mail you received")
    if plan.person:
        what.append(f"involving {plan.person}")
    if plan.since_hours:
        what.append(f"in the last {plan.since_hours} hour{'s' if plan.since_hours != 1 else ''}")
    elif plan.after:
        what.append(f"since {plan.after:%d %b}")
    if plan.kinds:
        what.append("in " + ", ".join(plan.kinds))
    detail = " ".join(what) or "your exact filters"
    return f"Nothing matched {detail}, so these are the closest results instead."
