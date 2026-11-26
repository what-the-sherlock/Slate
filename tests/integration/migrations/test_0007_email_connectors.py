"""Migration 0007 (ADR-0028) and the Fernet credential store, on a real Neon test branch.

Tests run in a rolled-back transaction, except the store test, which commits through the
unit of work and deletes its workspace at the end.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from core.adapters.postgres.credentials import PostgresCredentialStore, make_fernet
from core.adapters.postgres.uow import UnitOfWork
from core.domain.ids import UserId, WorkspaceId

pytestmark = pytest.mark.integration


@pytest.fixture
async def tx(direct_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    async with direct_engine.connect() as conn:
        trans = await conn.begin()
        try:
            yield conn
        finally:
            await trans.rollback()


async def _ws(conn: AsyncConnection) -> object:
    return (
        await conn.execute(
            text(
                "INSERT INTO workspaces (name, slug) VALUES ('m', 'm-' || gen_random_uuid()) RETURNING id"
            )
        )
    ).scalar_one()


async def _imap(
    conn: AsyncConnection, ws: object, address: str, *, cred: object = None, dc: str = "private"
) -> object:
    return (
        await conn.execute(
            text(
                "INSERT INTO sources (workspace_id, connector, name, config, visibility, data_class,"
                " credential_id) VALUES (:ws, 'imap', :n, CAST(:cfg AS jsonb), ARRAY['user:x'], :dc,"
                " :cred) RETURNING id"
            ),
            {
                "ws": ws,
                "n": f"Gmail · {uuid4().hex[:6]}",
                "cfg": f'{{"address": "{address}"}}',
                "dc": dc,
                "cred": cred,
            },
        )
    ).scalar_one()


async def test_imap_is_a_known_connector_and_always_private(tx: AsyncConnection) -> None:
    ws = await _ws(tx)
    await _imap(tx, ws, "a@gmail.com")
    with pytest.raises(DBAPIError):
        async with tx.begin_nested():
            await _imap(tx, ws, "b@gmail.com", dc="public")


async def test_one_connection_per_mailbox_per_workspace(tx: AsyncConnection) -> None:
    ws = await _ws(tx)
    await _imap(tx, ws, "Me@Gmail.com")
    with pytest.raises(DBAPIError):
        async with tx.begin_nested():
            await _imap(tx, ws, "me@gmail.com")  # case-insensitive
    await _imap(tx, await _ws(tx), "me@gmail.com")  # another workspace may


async def test_deleting_the_source_deletes_its_credential(tx: AsyncConnection) -> None:
    ws = await _ws(tx)
    cred = (
        await tx.execute(
            text(
                "INSERT INTO credentials (workspace_id, kind, secret_encrypted)"
                " VALUES (:ws, 'imap_app_password', 'x') RETURNING id"
            ),
            {"ws": ws},
        )
    ).scalar_one()
    sid = await _imap(tx, ws, "c@gmail.com", cred=cred)
    await tx.execute(text("DELETE FROM sources WHERE id = :s"), {"s": sid})
    left = (
        await tx.execute(text("SELECT count(*) FROM credentials WHERE id = :c"), {"c": cred})
    ).scalar_one()
    assert left == 0


@pytest.fixture
async def workspace_and_user(uow: UnitOfWork) -> AsyncIterator[tuple[WorkspaceId, UserId]]:
    """A workspace and a real user (credentials.created_by is a foreign key to users)."""
    async with uow() as s:
        wid = (
            await s.execute(
                text(
                    "INSERT INTO workspaces (name, slug) VALUES ('c', 'c-' || gen_random_uuid())"
                    " RETURNING id"
                )
            )
        ).scalar_one()
        uid = (
            await s.execute(
                text(
                    "INSERT INTO users (email, email_canonical, name, password_hash)"
                    " VALUES (:e, :e, 'c', 'h') RETURNING id"
                ),
                {"e": f"cred-{uuid4().hex[:8]}@it.test"},
            )
        ).scalar_one()
    yield WorkspaceId(UUID(str(wid))), UserId(UUID(str(uid)))
    async with uow() as s:
        await s.execute(text("DELETE FROM workspaces WHERE id = :w"), {"w": wid})
        await s.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})


async def test_credentials_are_ciphertext_scoped_and_rotatable(
    uow: UnitOfWork, workspace_and_user: tuple[WorkspaceId, UserId]
) -> None:
    workspace, user = workspace_and_user
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    store = PostgresCredentialStore(uow, make_fernet(old))
    cid = await store.save(
        workspace, kind="imap_app_password", secret="hunter2-app-pass", created_by=user
    )
    async with uow() as s:
        raw = (
            await s.execute(
                text("SELECT secret_encrypted FROM credentials WHERE id = :c"), {"c": cid}
            )
        ).scalar_one()
    assert b"hunter2" not in bytes(raw)  # only ciphertext is stored
    assert await store.secret(WorkspaceId(uuid4()), cid) is None  # another workspace
    rotated = PostgresCredentialStore(uow, make_fernet(f"{new},{old}"))
    assert await rotated.secret(workspace, cid) == "hunter2-app-pass"  # old key still decrypts
    gone = PostgresCredentialStore(uow, make_fernet(new))
    assert await gone.secret(workspace, cid) is None  # key rotated away: None, not a crash
    await store.delete(workspace, cid)
    assert await store.secret(workspace, cid) is None
