"""ConnectionStore over Postgres (ADR-0032). Tokens are one Fernet ciphertext per row
(``CREDENTIALS_FERNET_KEY``, rotatable via MultiFernet); plaintext never reaches the
database or the logs. Every user-facing query is scoped by workspace *and* owner."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import structlog
from cryptography.fernet import InvalidToken, MultiFernet
from sqlalchemy import text

from core.adapters.postgres.uow import UnitOfWork
from core.domain.ids import UserId, WorkspaceId
from core.domain.integrations import (
    Account,
    ConnectionStatus,
    TokenSet,
    secret_json,
    short_error,
    tokens_from_secret,
)
from core.ports.integrations import Connection

log = structlog.get_logger(__name__)

_COLUMNS = (
    "id, workspace_id, user_id, provider, account_id, account_label, scopes, status,"
    " expires_at, last_error, created_at"
)


class PostgresConnectionStore:
    def __init__(self, uow: UnitOfWork, fernet: MultiFernet | None) -> None:
        self._uow = uow
        self._fernet = fernet

    @property
    def available(self) -> bool:
        return self._fernet is not None

    def _encrypt(self, tokens: TokenSet) -> bytes:
        if self._fernet is None:
            raise RuntimeError("CREDENTIALS_FERNET_KEY is not configured")
        return self._fernet.encrypt(secret_json(tokens).encode())

    async def save(
        self, ws: WorkspaceId, user: UserId, provider: str, account: Account, tokens: TokenSet
    ) -> Connection:
        params = {
            "ws": ws,
            "u": user,
            "p": provider,
            "aid": account.id,
            "label": account.label,
            "scopes": list(tokens.scopes),
            "secret": self._encrypt(tokens),
            "exp": tokens.expires_at,
        }
        sql = (
            "INSERT INTO connections (workspace_id, user_id, provider, account_id,"  # noqa: S608
            " account_label, scopes, secret_encrypted, expires_at)"
            " VALUES (:ws, :u, :p, :aid, :label, CAST(:scopes AS text[]), :secret, :exp)"
            " ON CONFLICT (workspace_id, user_id, provider, account_id) DO UPDATE SET"
            " account_label = EXCLUDED.account_label, scopes = EXCLUDED.scopes,"
            " secret_encrypted = EXCLUDED.secret_encrypted, expires_at = EXCLUDED.expires_at,"
            " status = 'connected', last_error = NULL, refresh_lease_until = NULL,"
            " updated_at = now()"
            f" RETURNING {_COLUMNS}"
        )
        async with self._uow() as s:
            row = (await s.execute(text(sql), params)).mappings().one()
        return _connection(row)

    async def list_for_user(self, ws: WorkspaceId, user: UserId) -> list[Connection]:
        sql = (
            f"SELECT {_COLUMNS} FROM connections"  # noqa: S608 (constant columns)
            " WHERE workspace_id = :ws AND user_id = :u ORDER BY provider, created_at"
        )
        async with self._uow() as s:
            rows = (await s.execute(text(sql), {"ws": ws, "u": user})).mappings().all()
        return [_connection(r) for r in rows]

    async def get(self, ws: WorkspaceId, user: UserId, connection_id: UUID) -> Connection | None:
        sql = (
            f"SELECT {_COLUMNS} FROM connections"  # noqa: S608 (constant columns)
            " WHERE id = :id AND workspace_id = :ws AND user_id = :u"
        )
        params = {"id": connection_id, "ws": ws, "u": user}
        async with self._uow() as s:
            row = (await s.execute(text(sql), params)).mappings().first()
        return _connection(row) if row else None

    async def by_id(self, connection_id: UUID) -> Connection | None:
        sql = f"SELECT {_COLUMNS} FROM connections WHERE id = :id"  # noqa: S608
        async with self._uow() as s:
            row = (await s.execute(text(sql), {"id": connection_id})).mappings().first()
        return _connection(row) if row else None

    async def tokens(self, connection_id: UUID) -> TokenSet | None:
        if self._fernet is None:
            return None
        async with self._uow() as s:
            blob = (
                await s.execute(
                    text("SELECT secret_encrypted FROM connections WHERE id = :id"),
                    {"id": connection_id},
                )
            ).scalar_one_or_none()
        if blob is None:
            return None
        try:
            return tokens_from_secret(self._fernet.decrypt(bytes(blob)).decode())
        except InvalidToken:
            log.warning("connection_secret_undecryptable", connection_id=str(connection_id))
            return None

    async def try_lease(self, connection_id: UUID, until: datetime) -> bool:
        async with self._uow() as s:
            got = (
                await s.execute(
                    text(
                        "UPDATE connections SET refresh_lease_until = :until"
                        " WHERE id = :id AND (refresh_lease_until IS NULL"
                        "   OR refresh_lease_until < now()) RETURNING id"
                    ),
                    {"id": connection_id, "until": until},
                )
            ).first()
        return got is not None

    async def store_refresh(self, connection_id: UUID, tokens: TokenSet) -> None:
        async with self._uow() as s:
            await s.execute(
                text(
                    "UPDATE connections SET secret_encrypted = :secret, expires_at = :exp,"
                    " scopes = CASE WHEN cardinality(CAST(:scopes AS text[])) > 0"
                    "   THEN CAST(:scopes AS text[]) ELSE scopes END,"
                    " status = 'connected', last_error = NULL, refresh_lease_until = NULL,"
                    " last_refreshed_at = now(), updated_at = now() WHERE id = :id"
                ),
                {
                    "id": connection_id,
                    "secret": self._encrypt(tokens),
                    "exp": tokens.expires_at,
                    "scopes": list(tokens.scopes),
                },
            )

    async def mark(self, connection_id: UUID, status: ConnectionStatus, error: str | None) -> None:
        async with self._uow() as s:
            await s.execute(
                text(
                    "UPDATE connections SET status = :st, last_error = :err,"
                    " refresh_lease_until = NULL, updated_at = now() WHERE id = :id"
                ),
                {
                    "id": connection_id,
                    "st": status.value,
                    "err": short_error(error) if error else None,
                },
            )

    async def delete(self, ws: WorkspaceId, user: UserId, connection_id: UUID) -> bool:
        async with self._uow() as s:
            gone = (
                await s.execute(
                    text(
                        "DELETE FROM connections WHERE id = :id AND workspace_id = :ws"
                        " AND user_id = :u RETURNING id"
                    ),
                    {"id": connection_id, "ws": ws, "u": user},
                )
            ).first()
        return gone is not None


def _connection(r: Any) -> Connection:  # reason: RowMapping values are Any
    return Connection(
        id=UUID(str(r["id"])),
        workspace_id=WorkspaceId(UUID(str(r["workspace_id"]))),
        user_id=UserId(UUID(str(r["user_id"]))),
        provider=str(r["provider"]),
        account_id=str(r["account_id"]),
        account_label=r["account_label"],
        scopes=tuple(r["scopes"] or ()),
        status=ConnectionStatus(str(r["status"])),
        expires_at=r["expires_at"],
        last_error=r["last_error"],
        created_at=r["created_at"],
    )
