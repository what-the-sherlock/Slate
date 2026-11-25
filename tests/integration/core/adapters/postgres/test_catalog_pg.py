"""Sources read model on Postgres: permission filtering happens in SQL."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from core.adapters.postgres.catalog import PostgresCatalog
from core.adapters.postgres.sources import PostgresSourceStore
from core.adapters.postgres.uow import UnitOfWork
from core.domain.data_class import DataClass
from core.domain.ids import UserId, WorkspaceId
from core.domain.principals import Principals


@pytest.fixture
async def ws(uow: UnitOfWork) -> AsyncIterator[WorkspaceId]:
    async with uow() as s:
        wid = (
            await s.execute(
                text(
                    "INSERT INTO workspaces (name, slug) VALUES ('cat', 'cat-' || gen_random_uuid()) RETURNING id"
                )
            )
        ).scalar_one()
    yield WorkspaceId(UUID(str(wid)))
    async with uow() as s:
        await s.execute(text("DELETE FROM workspaces WHERE id = :w"), {"w": wid})


async def _doc(uow: UnitOfWork, ws: WorkspaceId, source: UUID, acl: str, dc: str) -> None:
    async with uow() as s:
        await s.execute(
            text(
                "INSERT INTO documents (workspace_id, source_id, external_id, uri, title, mime,"
                " checksum, acl, data_class) VALUES (:ws, :s, gen_random_uuid()::text, 'u', 't',"
                " 'text/plain', 'c', CAST(:acl AS text[]), :dc)"
            ),
            {"ws": ws, "s": source, "acl": acl, "dc": dc},
        )


async def test_guest_sees_only_public_sources_and_counts(uow: UnitOfWork, ws: WorkspaceId) -> None:
    store = PostgresSourceStore(uow)
    private = await store.upload_source(ws, DataClass.PRIVATE)
    public = await store.upload_source(ws, DataClass.PUBLIC)
    owner = UserId(uuid4())
    await _doc(uow, ws, private, f"{{user:{owner}}}", "private")
    await _doc(uow, ws, public, "{public}", "public")

    catalog = PostgresCatalog(uow)
    guest = Principals.for_outsider(UserId(uuid4()))
    member = Principals.for_user(owner, ws)

    guest_view = {s.name: s.documents for s in await catalog.sources(ws, guest)}
    assert guest_view == {"Public uploads": 1}  # the private source isn't even listed

    member_view = {s.name: s.documents for s in await catalog.sources(ws, member)}
    assert member_view == {"Public uploads": 1, "Uploads": 1}

    colleague = Principals.for_user(UserId(uuid4()), ws)
    colleague_view = {s.name: s.documents for s in await catalog.sources(ws, colleague)}
    assert colleague_view["Uploads"] == 0  # sees the source, not someone's only-me document


async def test_runs_of_invisible_sources_are_not_found(uow: UnitOfWork, ws: WorkspaceId) -> None:
    from core.domain.errors import NotFound

    private = await PostgresSourceStore(uow).upload_source(ws, DataClass.PRIVATE)
    guest = Principals.for_outsider(UserId(uuid4()))
    with pytest.raises(NotFound):
        await PostgresCatalog(uow).runs(ws, guest, private, 10)
