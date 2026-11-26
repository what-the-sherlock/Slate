from __future__ import annotations

from uuid import uuid4

import pytest

from core.domain.auth import Identity, Role
from core.domain.data_class import DataClass
from core.domain.errors import NotFound, UpstreamUnavailable
from core.domain.ids import JobId, SessionId, SourceId, UserId, WorkspaceId
from core.domain.principals import Principals, user_principal
from core.services.inline import InlineOutcome
from core.services.source_actions import ConnectorEntry, SourceActions
from tests.fakes.sources import InMemorySources


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
    _, outcome = await actions.sync_now(me, sid)
    assert outcome is InlineOutcome.DEFERRED
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
