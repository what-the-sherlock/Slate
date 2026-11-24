"""SourceStore over Postgres."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from core.adapters.postgres.uow import UnitOfWork
from core.domain.data_class import DataClass, classify
from core.domain.ids import DocumentId, RunId, SourceId, WorkspaceId
from core.domain.principals import PUBLIC, workspace_principal
from core.ports.sources import ExistingUpload

_UPLOAD_SOURCE_NAMES = {DataClass.PRIVATE: "Uploads", DataClass.PUBLIC: "Public uploads"}


class PostgresSourceStore:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def upload_source(self, ws: WorkspaceId, data_class: DataClass) -> SourceId:
        name = _UPLOAD_SOURCE_NAMES[data_class]
        select = (
            "SELECT id, connector, data_class FROM sources WHERE workspace_id = :ws AND name = :n"
        )
        async with self._uow() as s:
            # Read first: this runs on every upload, and a no-op upsert would still write.
            row = (await s.execute(text(select), {"ws": ws, "n": name})).first()
            if row is None:
                # Source visibility is the widest audience its documents may have; each
                # document carries its own (possibly narrower) ACL.
                if data_class is DataClass.PUBLIC:
                    visibility = [PUBLIC, workspace_principal(ws)]
                else:
                    visibility = [workspace_principal(ws)]
                await s.execute(
                    text(
                        "INSERT INTO sources"
                        " (workspace_id, connector, name, visibility, data_class)"
                        " VALUES (:ws, 'upload', :n, CAST(:v AS text[]), :dc)"
                        " ON CONFLICT (workspace_id, name) DO NOTHING"
                    ),
                    {"ws": ws, "n": name, "v": visibility, "dc": data_class.value},
                )
                row = (await s.execute(text(select), {"ws": ws, "n": name})).one()
        if row[1] != "upload" or row[2] != data_class.value:
            # A user-made source took the reserved name: refuse rather than mix data classes.
            raise RuntimeError(f"source name {name!r} is taken by a non-upload source")
        return SourceId(row[0])

    async def find_upload(self, ws: WorkspaceId, external_id: str) -> ExistingUpload | None:
        async with self._uow() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT d.id, d.acl, d.data_class FROM documents d"
                        " JOIN sources src ON src.id = d.source_id AND src.connector = 'upload'"
                        " WHERE d.workspace_id = :ws AND d.external_id = :e"
                        " AND d.deleted_at IS NULL LIMIT 1"
                    ),
                    {"ws": ws, "e": external_id},
                )
            ).first()
        if row is None:
            return None
        acl: Any = row[1]  # reason: text[] decodes to list[str]
        return ExistingUpload(
            DocumentId(row[0]),
            tuple(str(a) for a in acl),
            classify(row[2]),
        )

    async def create_run(self, source_id: SourceId, kind: str) -> RunId:
        async with self._uow() as s:
            rid = (
                await s.execute(
                    text(
                        "INSERT INTO ingestion_runs (workspace_id, source_id, kind, status,"
                        " started_at) SELECT workspace_id, id, :k, 'running', now()"
                        " FROM sources WHERE id = :s RETURNING id"
                    ),
                    {"s": source_id, "k": kind},
                )
            ).scalar_one()
        return RunId(rid)
