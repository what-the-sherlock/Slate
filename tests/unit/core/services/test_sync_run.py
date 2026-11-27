"""RunBoundary (P0-1): every way a sync can end is recorded honestly."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import uuid4

import pytest

from core.domain.data_class import DataClass
from core.domain.errors import BudgetExceeded, NotFound, RateLimited, UpstreamUnavailable
from core.domain.ids import SourceId, WorkspaceId
from core.domain.skips import SkipReason
from core.services.sync_run import INTERRUPTED, RunBoundary
from tests.fakes.sources import InMemorySources
from tests.fakes.storage import InMemoryIndex

SECRET = "Q3 salary bands: Ada 180k"  # stands in for document text inside an exception


@dataclass
class Tally:
    seen: int = 0
    changed: int = 0
    failed: int = 0


class Env:
    def __init__(self) -> None:
        self.sources = InMemorySources(InMemoryIndex())
        self.tally = Tally()
        self.cp: dict[str, object] = {"cursor": "start"}

    async def source(self) -> SourceId:
        return await self.sources.create_source(
            WorkspaceId(uuid4()), connector="github", name="r", config={},
            visibility=["user:x"], data_class=DataClass.PRIVATE,
        )  # fmt: skip

    async def boundary(self, sid: SourceId) -> RunBoundary:
        run = await self.sources.create_run(sid, "full")
        return RunBoundary(
            self.sources, sid, run, event="test.synced", counts=lambda: self.tally,
            checkpoint=lambda: dict(self.cp),
        )  # fmt: skip

    def outcome(self) -> tuple[str, str | None]:
        status, *_, summary = list(self.sources.finished.values())[-1]
        return status, summary


@pytest.fixture
async def env() -> Env:
    return Env()


async def test_a_clean_exit_succeeds_and_marks_the_source_synced(env: Env) -> None:
    sid = await env.source()
    async with await env.boundary(sid):
        env.tally.seen = env.tally.changed = 2
        env.cp = {"cursor": "end"}
    assert env.outcome() == ("succeeded", None)
    assert sid in env.sources.synced
    assert env.sources.records[sid].checkpoint == {"cursor": "end"}


async def test_an_unexpected_crash_is_failed_retried_and_leaks_no_content(env: Env) -> None:
    sid = await env.source()
    env.tally.seen = 3
    env.cp = {"cursor": "page-2"}  # progress committed before the crash
    boundary = await env.boundary(sid)

    async def crash() -> None:
        async with boundary:
            raise KeyError(SECRET)

    with pytest.raises(KeyError):  # re-raised: the queue retries, then marks it dead
        await crash()
    status, summary = env.outcome()
    assert status == "failed"
    assert summary
    assert "KeyError" in summary
    assert sid not in env.sources.synced  # last_synced_at doesn't move
    assert env.sources.records[sid].checkpoint == {"cursor": "page-2"}  # progress kept
    [(_, stage, message, retryable)] = env.sources.errors
    assert (stage, retryable) == ("fetch", True)
    stored = " ".join([message, summary])
    assert SECRET not in stored
    assert "salary" not in stored


@pytest.mark.parametrize("exc", [ValueError("x"), TypeError("x"), AttributeError("x")])
async def test_any_exception_type_is_a_failure_not_a_success(env: Env, exc: Exception) -> None:
    sid = await env.source()
    with pytest.raises(type(exc)):
        async with await env.boundary(sid):
            raise exc
    assert env.outcome()[0] == "failed"


async def test_typed_errors_fail_with_their_reason_and_are_not_re_raised(env: Env) -> None:
    sid = await env.source()
    async with await env.boundary(sid):
        raise NotFound("That repository isn't available to Slate.")
    assert env.outcome() == ("failed", "That repository isn't available to Slate.")
    assert env.sources.errors[-1][3] is False  # not retryable
    async with await env.boundary(sid):
        raise UpstreamUnavailable("GitHub is unavailable right now.")
    assert env.sources.errors[-1][3] is True
    assert sid not in env.sources.synced


async def test_a_spent_budget_is_partial_and_resumable(env: Env) -> None:
    sid = await env.source()
    async with await env.boundary(sid):
        raise BudgetExceeded("This month's AI budget is spent.")
    assert env.outcome() == ("partial", "This month's AI budget is spent.")


async def test_rate_limits_are_partial_then_re_raised_for_backoff(env: Env) -> None:
    sid = await env.source()
    with pytest.raises(RateLimited):
        async with await env.boundary(sid):
            raise RateLimited("slow down", retry_after_s=60)
    assert env.outcome() == ("partial", "slow down")


async def test_cancellation_is_never_recorded_as_success(env: Env) -> None:
    # The inline runner's asyncio.timeout cancels the handler: CancelledError is a
    # BaseException, so an `except Exception` boundary used to miss it.
    sid = await env.source()

    async def body() -> None:
        async with await env.boundary(sid):
            await asyncio.sleep(10)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await body()
    assert env.outcome() == ("partial", INTERRUPTED)
    assert sid not in env.sources.synced


async def test_failed_items_turn_success_into_partial(env: Env) -> None:
    sid = await env.source()
    async with await env.boundary(sid):
        env.tally.seen, env.tally.failed = 5, 2
    assert env.outcome() == ("partial", "2 item(s) couldn't be indexed.")
    # Still "synced" for scheduling: otherwise one permanently broken file would make the
    # source due on every worker pass. The engine's held-back checkpoint retries it.
    assert sid in env.sources.synced


async def test_skips_are_stored_on_the_run(env: Env) -> None:
    sid = await env.source()
    async with await env.boundary(sid) as b:
        b.skips.add(SkipReason.UNSUPPORTED_TYPE, "pdf", 30)
        b.skips.merge({"too_large:file": 2, "bogus_reason": 9, "empty_text": True})
    [skipped] = env.sources.skipped.values()
    assert skipped == {"too_large:file": 2, "unsupported_type:pdf": 30}  # junk dropped


async def test_engine_set_outcomes_stand(env: Env) -> None:
    sid = await env.source()
    async with await env.boundary(sid) as b:
        b.partial("Still syncing: the rest continues in the background.")
    assert env.outcome()[0] == "partial"
    async with await env.boundary(sid) as b:
        await b.fail("The bot can't read messages.")
    assert env.outcome() == ("failed", "The bot can't read messages.")
