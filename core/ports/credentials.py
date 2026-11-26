"""Encrypted connector secrets (ARCHITECTURE §4 ``credentials``). Only ciphertext is stored;
the key lives in env (``CREDENTIALS_FERNET_KEY``). Secrets never leave the adapter except
to the connector that needs them, and are never logged."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from core.domain.ids import UserId, WorkspaceId


class CredentialStore(Protocol):
    @property
    def available(self) -> bool:
        """False without an encryption key: features needing a stored secret are hidden."""
        ...

    async def save(
        self, ws: WorkspaceId, *, kind: str, secret: str, created_by: UserId
    ) -> UUID: ...

    async def secret(self, ws: WorkspaceId, credential_id: UUID) -> str | None:
        """The decrypted secret, or None if it's gone, in another workspace, or no longer
        decrypts (the key was rotated away)."""
        ...

    async def update(self, ws: WorkspaceId, credential_id: UUID, secret: str) -> None:
        """Replace the secret (a refreshed token)."""
        ...

    async def delete(self, ws: WorkspaceId, credential_id: UUID) -> None: ...

    async def sweep_unreferenced(self, kind: str, older_than_hours: int) -> int:
        """Delete credentials of ``kind`` no source uses any more (an abandoned connect, a
        reconnect that replaced them). Returns how many went."""
        ...
