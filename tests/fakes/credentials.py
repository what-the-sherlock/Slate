from __future__ import annotations

from uuid import UUID, uuid4

from core.domain.ids import UserId, WorkspaceId


class InMemoryCredentials:
    def __init__(self, *, available: bool = True) -> None:
        self._available = available
        self.rows: dict[UUID, tuple[WorkspaceId, str, str]] = {}  # id -> (ws, kind, secret)

    @property
    def available(self) -> bool:
        return self._available

    async def save(self, ws: WorkspaceId, *, kind: str, secret: str, created_by: UserId) -> UUID:
        cid = uuid4()
        self.rows[cid] = (ws, kind, secret)
        return cid

    async def secret(self, ws: WorkspaceId, credential_id: UUID) -> str | None:
        row = self.rows.get(credential_id)
        return row[2] if row is not None and row[0] == ws else None

    async def update(self, ws: WorkspaceId, credential_id: UUID, secret: str) -> None:
        row = self.rows.get(credential_id)
        if row is not None and row[0] == ws:
            self.rows[credential_id] = (row[0], row[1], secret)

    async def sweep_unreferenced(self, kind: str, older_than_hours: int) -> int:
        return 0

    async def delete(self, ws: WorkspaceId, credential_id: UUID) -> None:
        row = self.rows.get(credential_id)
        if row is not None and row[0] == ws:
            del self.rows[credential_id]
