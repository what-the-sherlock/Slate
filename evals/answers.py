"""Answer evals (a composition root): the real AnswerService over the eval cases.

    uv run python -m evals.answers --limit 10      # a sample first
    uv run python -m evals.answers                 # all 51+ cases

PAID: each answer calls OpenAI's chat model (gpt-4o-mini by default) and embeds the query.
About $0.001 per case, so the full set costs roughly $0.05-0.10. Refused if this month's
budget or today's global allowance couldn't cover it (EVALS.md: evals stay under $1/month).
Everything is written to the usage ledger under the eval workspace.

Writes evals/results/<date>_<sha>_answers.json and prints abstention accuracy, citation
validity and point coverage. Chats are kept in memory: the eval principals aren't real
users, and eval conversations shouldn't be stored.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from core.adapters.factories import openai_embedder
from core.adapters.openai.chat import DEFAULT_MODEL, OpenAIChatModel
from core.adapters.postgres.engine import (
    EngineKind,
    create_engine,
    create_session_factory,
    use_selector_loop_on_windows,
)
from core.adapters.postgres.evals import PostgresEvalStore
from core.adapters.postgres.search import PostgresSearchIndex
from core.adapters.postgres.uow import make_uow
from core.adapters.postgres.usage import PostgresGenerationStore, PostgresUsageLedger
from core.adapters.tokenizer import TiktokenTokenizer
from core.config import get_settings
from core.domain.auth import Identity, Role
from core.domain.budget import SpendLimits
from core.domain.ids import ChatSessionId, DocumentId, MessageId, SessionId, UserId, WorkspaceId
from core.domain.principals import Principals
from core.ports.chat import ChatSessionInfo, Citation, NewMessage, StoredMessage
from core.ports.rate_limit import Limit
from core.services.answer import AnswerService, Cited, Done, Token
from core.services.search import SearchService
from evals.answer_metrics import AnswerResult, points_found, summarise_answers
from evals.cases import EXTERNAL_PREFIX, check_references, load_cases, load_corpus
from evals.harness import EVAL_OWNER
from evals.load import EVAL_WORKSPACE_NAME, EVAL_WORKSPACE_SLUG
from evals.run import RESULTS, git_sha

PER_CASE_ESTIMATE_USD = Decimal("0.002")


class _Unlimited:
    """Rate limits are for people; an eval run is bounded by --limit and the budget."""

    async def try_spend(self, key: str, cost: int, limit: Limit) -> bool:
        return True


class _MemoryChats:
    """A throwaway ChatStore: one fresh chat per case, nothing persisted."""

    def __init__(self) -> None:
        self._messages: dict[ChatSessionId, list[StoredMessage]] = {}

    async def create_session(
        self, ws: WorkspaceId, user: UserId, title: str | None
    ) -> ChatSessionId:
        sid = ChatSessionId(uuid4())
        self._messages[sid] = []
        return sid

    async def get_session(
        self, ws: WorkspaceId, user: UserId, session: ChatSessionId
    ) -> ChatSessionInfo | None:
        return (
            ChatSessionInfo(session, None, datetime.now(UTC)) if session in self._messages else None
        )

    async def list_sessions(
        self, ws: WorkspaceId, user: UserId, *, limit: int, before: datetime | None
    ) -> list[ChatSessionInfo]:
        return []

    async def messages(
        self, ws: WorkspaceId, user: UserId, session: ChatSessionId, *, limit: int
    ) -> list[StoredMessage]:
        return self._messages.get(session, [])[-limit:]

    async def add_message(
        self,
        ws: WorkspaceId,
        user: UserId,
        session: ChatSessionId,
        message: NewMessage,
        *,
        supersede_from: MessageId | None = None,  # evals ask one-shot questions
    ) -> MessageId:
        mid = MessageId(uuid4())
        self._messages.setdefault(session, []).append(
            StoredMessage(mid, message.role, message.content, datetime.now(UTC))
        )
        return mid

    async def add_citations(self, message: MessageId, citations: Sequence[Citation]) -> None:
        return None

    async def save_feedback(
        self,
        ws: WorkspaceId,
        user: UserId,
        message: MessageId,
        *,
        rating: int,
        reason: str | None,
        comment: str | None,
    ) -> None:
        return None


async def run(limit: int | None, only: set[str] | None = None) -> int:
    settings = get_settings()
    key = settings.openai_api_key.get_secret_value().strip() if settings.openai_api_key else ""
    embedder = openai_embedder(settings)
    if not key or embedder is None:
        raise SystemExit("OPENAI_API_KEY isn't configured: answer evals need it (they're paid)")
    cases = load_cases()
    corpus = load_corpus()
    check_references(cases, corpus)
    if only:
        unknown = only - {c.id for c in cases}
        if unknown:
            raise SystemExit(f"unknown case ids: {', '.join(sorted(unknown))}")
        cases = [c for c in cases if c.id in only]
    cases = cases[:limit] if limit else cases

    engine = create_engine(settings.require_database_url(direct=True), EngineKind.DIRECT)
    try:
        uow = make_uow(create_session_factory(engine))
        store = PostgresEvalStore(uow)
        ws = await store.ensure_workspace(EVAL_WORKSPACE_SLUG, EVAL_WORKSPACE_NAME)
        ids = await store.document_ids(ws, [d.external_id for d in corpus])
        if len(ids) != len(corpus):
            raise SystemExit(
                "the eval corpus isn't fully loaded: run `uv run python -m evals.load`"
            )
        path_of = {v: ext.removeprefix(EXTERNAL_PREFIX) for ext, v in ids.items()}

        ledger = PostgresUsageLedger(uow)
        limits = SpendLimits(
            monthly_all=Decimal(str(settings.monthly_llm_budget_usd)),
            daily_all=Decimal(str(settings.daily_llm_budget_usd)),
        )
        estimate = PER_CASE_ESTIMATE_USD * len(cases)
        if reason := limits.refusal(await ledger.spend(ws), estimate):
            raise SystemExit(f"not running ~${estimate} of answer evals: {reason}")

        search = SearchService(
            PostgresSearchIndex(uow),
            embedder,
            PostgresGenerationStore(uow),
            ledger,
            _Unlimited(),
            limits=limits,
        )
        model = OpenAIChatModel(key, model=settings.openai_chat_model or DEFAULT_MODEL)
        answers = AnswerService(
            search, model, _MemoryChats(), ledger, _Unlimited(), TiktokenTokenizer(),
            limits=limits, min_similarity=settings.chat_min_similarity,
        )  # fmt: skip
        who = Identity(
            user_id=EVAL_OWNER,
            workspace_id=ws,
            session_id=SessionId(uuid4()),
            email="evals@slate.invalid",
            name="Evals",
            role=Role.OWNER,
            principals=Principals.for_user(EVAL_OWNER, ws),
            csrf_secret="",
            email_verified=True,
        )

        results: list[AnswerResult] = []
        for case in cases:
            before = (await ledger.spend(ws)).month_workspace
            question, _ = await answers.prepare(who, case.question, None)
            cited: list[DocumentId] = []
            text, done = "", None
            async for event in answers.answer(who, question, None):
                if isinstance(event, Cited):
                    cited.append(event.document_id)
                elif isinstance(event, Done):
                    done = event
                elif isinstance(event, Token):
                    text += event.text
            spent = (await ledger.spend(ws)).month_workspace - before
            results.append(
                AnswerResult(
                    id=case.id,
                    answerable=case.answerable,
                    abstained=bool(done and done.abstained),
                    cited_documents=[path_of.get(d, str(d)) for d in cited],
                    expected_documents=list(case.expected_documents),
                    points_expected=list(case.expected_answer_points),
                    points_found=points_found(text, case.expected_answer_points),
                    cost_usd=float(spent),
                    latency_ms=done.latency_ms if done else 0,
                    text=text,
                    expect_mode=case.expect_mode,
                    mode=done.mode if done else "grounded",
                )
            )
            print(f"  {case.id:<12} {'ABSTAINED' if results[-1].abstained else 'answered'}")
    finally:
        await engine.dispose()

    summary = summarise_answers(results)
    sha = git_sha()
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{datetime.now(UTC):%Y-%m-%d}_{sha}_answers.json"
    payload = {
        "git_sha": sha,
        "model": model.model,
        "summary": summary,
        "cases": [asdict(r) for r in results],
    }
    out.write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"\n{json.dumps(summary, indent=2)}\nWrote {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m evals.answers")
    p.add_argument("--limit", type=int, default=None, help="run only the first N cases")
    p.add_argument("--ids", default=None, help="comma-separated case ids to run (cheap re-checks)")
    p.add_argument(
        "--rescore", type=Path, default=None, help="re-score a saved results file ($0, no calls)"
    )
    args = p.parse_args(argv)
    if args.rescore is not None:
        return rescore(args.rescore)
    use_selector_loop_on_windows()
    ids = {i.strip() for i in args.ids.split(",")} if args.ids else None
    return asyncio.run(run(args.limit, ids))


def rescore(path: Path) -> int:
    """Recompute the summary from stored answers with the current scoring rules."""
    data = json.loads(path.read_text(encoding="utf-8"))
    results = [AnswerResult(**c) for c in data["cases"]]
    results = [
        AnswerResult(**{**asdict(r), "points_found": points_found(r.text, r.points_expected)})
        for r in results
    ]
    print(json.dumps(summarise_answers(results), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
