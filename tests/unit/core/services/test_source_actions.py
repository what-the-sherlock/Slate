from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import pytest

from core.domain.auth import Identity, Role
from core.domain.data_class import DataClass
from core.domain.errors import NotFound, UpstreamUnavailable
from core.domain.ids import JobId, SessionId, SourceId, UserId, WorkspaceId
from core.domain.jobs import Job
from core.domain.principals import Principals, user_principal
from core.domain.skips import SkipReason
from core.domain.source_status import WAITING_FOR_WORKER
from core.services.inline import InlineRunner
from core.services.source_actions import (
    CRASHED,
    ConnectorEntry,
    SourceActions,
    SyncStatus,
)
from core.services.sync_run import RunBoundary
from tests.fakes.jobs import FakeJobQueue
from tests.fakes.sources import InMemorySources
from tests.fakes.system import FakeClock


def identity(ws: WorkspaceId, role: Role = Role.OWNER) -> Identity:
    u = UserId(uuid4())
    return Identity(
        user_id=u, workspace_id=ws, session_id=SessionId(uuid4()), email="a@b.c", name="A",
        role=role, principals=Principals.for_user(u, ws), csrf_secret="c", email_verified=True,
    )  # fmt: skip


class Recorder:
    def __init__(self, available: bool = True) -> None:
        self._available = available
        self.synced: list[SourceId] = []
        self.deleted: list[SourceId] = []

    @property
    def available(self) -> bool:
        return self._available

    async def sync_now(self, identity: Identity, source_id: SourceId) -> JobId:
        self.synced.append(source_id)
        return JobId(uuid4())

    async def disconnect(self, identity: Identity, source_id: SourceId) -> None:
        self.deleted.append(source_id)


async def _source(store: InMemorySources, who: Identity, connector: str = "discord") -> SourceId:
    return await store.create_source(
        who.workspace_id, connector=connector, name=str(uuid4()), config={},
        visibility=[user_principal(who.user_id)], data_class=DataClass.PRIVATE,
        created_by=who.user_id,
    )  # fmt: skip


async def test_dispatches_by_connector() -> None:
    store, rec = InMemorySources(), Recorder()
    me = identity(WorkspaceId(uuid4()))
    sid = await _source(store, me)
    actions = SourceActions(store, {"discord": ConnectorEntry("Discord", rec)})
    result = await actions.sync_now(me, sid)
    assert result.status is SyncStatus.BACKGROUND  # no inline runner: the worker does it
    await actions.disconnect(me, sid)
    assert rec.synced == [sid]
    assert rec.deleted == [sid]


async def test_a_source_you_cannot_see_does_not_exist_for_you() -> None:
    store, rec = InMemorySources(), Recorder()
    ws = WorkspaceId(uuid4())
    owner, colleague = identity(ws), identity(ws)
    sid = await _source(store, owner)
    actions = SourceActions(store, {"discord": ConnectorEntry("Discord", rec)})
    for who in (colleague, identity(WorkspaceId(uuid4()))):
        with pytest.raises(NotFound):
            await actions.sync_now(who, sid)
        with pytest.raises(NotFound):
            await actions.disconnect(who, sid)
    assert rec.synced == []
    assert rec.deleted == []


async def test_unknown_connector_and_upload_sources_404() -> None:
    store = InMemorySources()
    me = identity(WorkspaceId(uuid4()))
    sid = await _source(store, me, connector="gmail_mbox")
    actions = SourceActions(store, {})
    with pytest.raises(NotFound):
        await actions.sync_now(me, sid)
    with pytest.raises(NotFound):
        await actions.sync_now(me, SourceId(uuid4()))


async def test_sync_needs_configuration_but_disconnect_never_does() -> None:
    store, rec = InMemorySources(), Recorder(available=False)
    me = identity(WorkspaceId(uuid4()))
    sid = await _source(store, me)
    actions = SourceActions(store, {"discord": ConnectorEntry("Discord", rec)})
    with pytest.raises(UpstreamUnavailable, match="Discord"):
        await actions.sync_now(me, sid)
    await actions.disconnect(me, sid)  # deleting your data never depends on the integration
    assert rec.deleted == [sid]


# ---------------------------------------------------------------- honest sync results (P0-2)


class Queued(Recorder):
    """Queues a real job, so the inline runner can claim and run it."""

    def __init__(self, queue: FakeJobQueue) -> None:
        super().__init__()
        self._queue = queue

    async def sync_now(self, identity: Identity, source_id: SourceId) -> JobId:
        return await self._queue.enqueue(
            identity.workspace_id, "sync_discord", {"source_id": str(source_id)}, str(uuid4())
        )


@dataclass
class _Tally:
    seen: int = 0
    changed: int = 0
    failed: int = 0


async def _sync_with(behaviour: str) -> tuple[SyncStatus, str | None]:
    store, queue = InMemorySources(), FakeJobQueue(FakeClock())
    me = identity(WorkspaceId(uuid4()))
    sid = await _source(store, me)

    async def handler(job: Job) -> None:
        if behaviour == "crash_before_run":
            raise KeyError("x")
        run = await store.create_run(sid, "full")
        tally = _Tally()
        async with RunBoundary(
            store, sid, run, event="t", counts=lambda: tally, checkpoint=None
        ) as b:
            if behaviour == "typed_failure":
                raise NotFound("That repository isn't available to Slate.")
            if behaviour == "crash":
                raise KeyError("document text")
            if behaviour == "partial":
                b.partial("Still syncing: the rest continues in the background.")

    runner = InlineRunner(queue, handler, runner_id="api-inline")
    actions = SourceActions(store, {"discord": ConnectorEntry("Discord", Queued(queue), runner)})
    result = await actions.sync_now(me, sid)
    return result.status, result.reason


@pytest.mark.parametrize(
    ("behaviour", "expected"),
    [
        ("ok", (SyncStatus.DONE, None)),
        ("partial", (SyncStatus.PARTIAL, "Still syncing: the rest continues in the background.")),
        # The job "finished" but its run failed: that's a failure, not "done" (P0-2).
        ("typed_failure", (SyncStatus.FAILED, "That repository isn't available to Slate.")),
        ("crash_before_run", (SyncStatus.FAILED, CRASHED)),
    ],
)
async def test_sync_now_reports_what_the_run_actually_did(
    behaviour: str, expected: tuple[SyncStatus, str | None]
) -> None:
    assert await _sync_with(behaviour) == expected


async def test_a_crash_reports_the_runs_reason_and_never_background() -> None:
    status, reason = await _sync_with("crash")
    assert status is SyncStatus.FAILED
    assert reason
    assert "KeyError" in reason
    assert "document text" not in reason


async def test_background_is_promised_only_while_the_worker_keeps_up() -> None:
    store, rec = InMemorySources(), Recorder()
    me = identity(WorkspaceId(uuid4()))
    sid = await _source(store, me)
    actions = SourceActions(store, {"discord": ConnectorEntry("Discord", rec)})
    assert (await actions.sync_now(me, sid)).status is SyncStatus.BACKGROUND
    store.overdue = True  # earlier jobs have waited 2 h+: nobody is processing them
    result = await actions.sync_now(me, sid)
    assert (result.status, result.reason) == (SyncStatus.FAILED, WAITING_FOR_WORKER)


async def test_a_done_sync_says_what_it_left_out() -> None:
    store, queue = InMemorySources(), FakeJobQueue(FakeClock())
    me = identity(WorkspaceId(uuid4()))
    sid = await _source(store, me)

    async def handler(job: Job) -> None:
        run = await store.create_run(sid, "full")
        async with RunBoundary(store, sid, run, event="t", counts=_Tally, checkpoint=None) as b:
            b.skips.add(SkipReason.UNSUPPORTED_TYPE, "code", 12)

    runner = InlineRunner(queue, handler, runner_id="api-inline")
    actions = SourceActions(store, {"discord": ConnectorEntry("Discord", Queued(queue), runner)})
    result = await actions.sync_now(me, sid)
    assert result.status is SyncStatus.DONE
    assert result.skipped == "12 code and other files (not supported yet)"
