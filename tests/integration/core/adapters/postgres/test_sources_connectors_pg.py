"""SourceStore methods added for Discord (ADR-0027), against real Postgres.

Committed rows (the store opens its own transactions), so each test cleans up its
workspace; deleting it cascades to sources, documents and runs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from core.adapters.postgres.documents import PostgresDocumentStore
from core.adapters.postgres.sources import PostgresSourceStore
from core.adapters.postgres.uow import UnitOfWork
from core.domain.data_class import DataClass
from core.domain.documents import DocumentUpsert
from core.domain.ids import RunId, WorkspaceId

pytestmark = pytest.mark.integration


@pytest.fixture
async def two_workspaces(uow: UnitOfWork) -> AsyncIterator[tuple[WorkspaceId, WorkspaceId]]:
    ids: list[WorkspaceId] = []
    async with uow() as s:
        for _ in range(2):
            wid = (
                await s.execute(
                    text(
                        "INSERT INTO workspaces (name, slug) VALUES ('c', 'c-' || gen_random_uuid())"
                        " RETURNING id"
                    )
                )
            ).scalar_one()
            ids.append(WorkspaceId(UUID(str(wid))))
    yield ids[0], ids[1]
    async with uow() as s:
        await s.execute(text("DELETE FROM workspaces WHERE id = ANY(:w)"), {"w": ids})


async def _discord(store: PostgresSourceStore, ws: WorkspaceId, guild: str) -> object:
    return await store.create_source(
        ws, connector="discord", name=f"Acme {uuid4().hex[:6]}", config={"guild_id": guild},
        visibility=[f"workspace:{ws}"], data_class=DataClass.PRIVATE,
    )  # fmt: skip


async def test_config_in_use_spans_workspaces_but_reveals_only_a_boolean(
    uow: UnitOfWork, two_workspaces: tuple[WorkspaceId, WorkspaceId]
) -> None:
    a, b = two_workspaces
    store = PostgresSourceStore(uow)
    guild = str(10**17 + int(uuid4().int % 10**9))
    sid = await _discord(store, a, guild)
    assert await store.config_in_use("discord", "guild_id", guild)
    assert not await store.config_in_use("github", "guild_id", guild)
    await _discord(store, b, guild)
    assert await store.delete_source(a, sid)  # type: ignore[arg-type]
    assert await store.config_in_use("discord", "guild_id", guild)  # still used by b
    assert not await store.config_in_use("discord", "guild_id", "1" + guild)


async def test_update_config_is_scoped_to_the_workspace(
    uow: UnitOfWork, two_workspaces: tuple[WorkspaceId, WorkspaceId]
) -> None:
    a, b = two_workspaces
    store = PostgresSourceStore(uow)
    sid = await _discord(store, a, "100000000000000000")
    assert not await store.update_config(b, sid, {"guild_id": "x"})  # type: ignore[arg-type]
    assert await store.update_config(a, sid, {"guild_id": "y", "channels": [{"id": "1"}]})  # type: ignore[arg-type]
    rec = await store.get_source(a, sid)  # type: ignore[arg-type]
    assert rec is not None
    assert rec.config["guild_id"] == "y"


async def test_document_versions_and_record_error_stage_fallback(
    uow: UnitOfWork, two_workspaces: tuple[WorkspaceId, WorkspaceId]
) -> None:
    a, _ = two_workspaces
    store, docs = PostgresSourceStore(uow), PostgresDocumentStore(uow)
    sid = await _discord(store, a, "100000000000000001")
    when = datetime(2026, 11, 20, tzinfo=UTC)
    for ext in ("msg:1:2", "thread:1:3"):
        await docs.upsert_document(
            a,
            DocumentUpsert(
                source_id=sid, external_id=ext, uri="u", title="t", mime="text/plain",  # type: ignore[arg-type]
                checksum=f"sum-{ext}", acl=(f"workspace:{a}",), source_updated_at=when,
            ),
        )  # fmt: skip
    versions = await store.document_versions(sid, "thread:")  # type: ignore[arg-type]
    assert versions == {"thread:1:3": ("sum-thread:1:3", when)}
    run = await store.create_run(sid, "full")  # type: ignore[arg-type]
    # An unknown stage must not violate ingestion_errors' CHECK (and crash the run).
    await store.record_error(
        RunId(run), external_id="x", stage="index", message="m", retryable=False
    )
    async with uow() as s:
        stage = (
            await s.execute(
                text("SELECT stage FROM ingestion_errors WHERE run_id = :r"), {"r": run}
            )
        ).scalar_one()
    assert stage == "store"
