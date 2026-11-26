"""CredentialStore over Postgres + Fernet (authenticated symmetric encryption).

``CREDENTIALS_FERNET_KEY`` may hold several comma-separated keys: the first encrypts, all
decrypt (MultiFernet), so a key can be rotated without breaking existing connections.
Rows are scoped by workspace. Plaintext never touches the database or the logs.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import text

from core.adapters.postgres.uow import UnitOfWork
from core.domain.ids import UserId, WorkspaceId

log = structlog.get_logger(__name__)


def make_fernet(raw_keys: str | None) -> MultiFernet | None:
    """None when no key is set or a key is malformed (logged, never echoed)."""
    keys = [k.strip() for k in (raw_keys or "").split(",") if k.strip()]
    if not keys:
        return None
    try:
        return MultiFernet([Fernet(k.encode()) for k in keys])
    except (ValueError, TypeError):
        log.error("credentials_key_invalid")
        return None


class PostgresCredentialStore:
    def __init__(self, uow: UnitOfWork, fernet: MultiFernet | None) -> None:
        self._uow = uow
        self._fernet = fernet

    @property
    def available(self) -> bool:
        return self._fernet is not None

    def _require(self) -> MultiFernet:
        if self._fernet is None:
            raise RuntimeError("CREDENTIALS_FERNET_KEY is not configured")
        return self._fernet

    async def save(self, ws: WorkspaceId, *, kind: str, secret: str, created_by: UserId) -> UUID:
        token = self._require().encrypt(secret.encode())
        async with self._uow() as s:
            cid = (
                await s.execute(
                    text(
                        "INSERT INTO credentials (workspace_id, kind, secret_encrypted, created_by)"
                        " VALUES (:ws, :k, :t, :by) RETURNING id"
                    ),
                    {"ws": ws, "k": kind, "t": token, "by": created_by},
                )
            ).scalar_one()
        return UUID(str(cid))

    async def secret(self, ws: WorkspaceId, credential_id: UUID) -> str | None:
        async with self._uow() as s:
            token = (
                await s.execute(
                    text(
                        "SELECT secret_encrypted FROM credentials"
                        " WHERE id = :id AND workspace_id = :ws"
                    ),
                    {"id": credential_id, "ws": ws},
                )
            ).scalar_one_or_none()
        if token is None:
            return None
        try:
            return self._require().decrypt(bytes(token)).decode()
        except InvalidToken:
            log.warning("credential_undecryptable")  # key rotated away: reconnect needed
            return None

    async def update(self, ws: WorkspaceId, credential_id: UUID, secret: str) -> None:
        token = self._require().encrypt(secret.encode())
        async with self._uow() as s:
            await s.execute(
                text(
                    "UPDATE credentials SET secret_encrypted = :t"
                    " WHERE id = :id AND workspace_id = :ws"
                ),
                {"t": token, "id": credential_id, "ws": ws},
            )

    async def sweep_unreferenced(self, kind: str, older_than_hours: int) -> int:
        async with self._uow() as s:
            result = await s.execute(
                text(
                    "DELETE FROM credentials c WHERE c.kind = :k"
                    " AND c.created_at < now() - make_interval(hours => :h)"
                    " AND NOT EXISTS (SELECT 1 FROM sources s WHERE s.credential_id = c.id)"
                ),
                {"k": kind, "h": older_than_hours},
            )
        return int(getattr(result, "rowcount", 0) or 0)

    async def delete(self, ws: WorkspaceId, credential_id: UUID) -> None:
        async with self._uow() as s:
            await s.execute(
                text("DELETE FROM credentials WHERE id = :id AND workspace_id = :ws"),
                {"id": credential_id, "ws": ws},
            )
