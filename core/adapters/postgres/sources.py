"""SourceStore over Postgres."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from core.adapters.postgres.uow import UnitOfWork
from core.domain.data_class import DataClass, classify
from core.domain.errors import Conflict
from core.domain.ids import DocumentId, RunId, SourceId, UserId, WorkspaceId
from core.domain.principals import PUBLIC, workspace_principal
from core.ports.sources import ExistingUpload, SourceRecord

_SOURCE_COLS = (
    "id, workspace_id, connector, name, visibility, data_class, status, config, checkpoint,"
    " last_synced_at, created_by, credential_id, connection_id"
)


_GET_SOURCE = f"SELECT {_SOURCE_COLS} FROM sources WHERE id = :id AND workspace_id = :ws"  # noqa: S608


def _source(r: Any) -> SourceRecord:  # reason: RowMapping values are Any
    return SourceRecord(
        id=SourceId(r["id"]),
        workspace_id=WorkspaceId(r["workspace_id"]),
        connector=str(r["connector"]),
        name=str(r["name"]),
        visibility=tuple(r["visibility"]),
        data_class=str(r["data_class"]),
        status=str(r["status"]),
        config=dict(r["config"] or {}),
        checkpoint=dict(r["checkpoint"]) if r["checkpoint"] is not None else None,
        last_synced_at=r["last_synced_at"],
        created_by=UserId(r["created_by"]) if r["created_by"] is not None else None,
        credential_id=r["credential_id"],
        connection_id=r["connection_id"],
    )


_ERROR_STAGES = frozenset({"fetch", "parse", "chunk", "embed", "store"})  # 0001's CHECK
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

    async def finish_run(
        self,
        run_id: RunId,
        *,
        status: str,
        seen: int,
        changed: int,
        failed: int,
        error_summary: str | None = None,
    ) -> None:
        async with self._uow() as s:
            await s.execute(
                text(
                    "UPDATE ingestion_runs SET status = :st, finished_at = now(),"
                    " docs_seen = :seen, docs_changed = :ch, docs_failed = :f,"
                    " error_summary = :err WHERE id = :id"
                ),
                {
                    "st": status,
                    "seen": seen,
                    "ch": changed,
                    "f": failed,
                    "err": error_summary,
                    "id": run_id,
                },
            )
            if status in {"succeeded", "partial"}:
                await s.execute(
                    text(
                        "UPDATE sources SET last_synced_at = now() WHERE id ="
                        " (SELECT source_id FROM ingestion_runs WHERE id = :id)"
                    ),
                    {"id": run_id},
                )

    async def progress(self, run_id: RunId, *, seen: int, changed: int, failed: int) -> None:
        async with self._uow() as s:
            await s.execute(
                text(
                    "UPDATE ingestion_runs SET docs_seen = :seen, docs_changed = :ch,"
                    " docs_failed = :f WHERE id = :id AND status = 'running'"
                ),
                {"seen": seen, "ch": changed, "f": failed, "id": run_id},
            )

    async def record_error(
        self, run_id: RunId, *, external_id: str | None, stage: str, message: str, retryable: bool
    ) -> None:
        # Defence in depth: an unknown stage would violate the CHECK and lose the error
        # *and* crash the run that was trying to record it.
        if stage not in _ERROR_STAGES:
            stage = "store"
        async with self._uow() as s:
            await s.execute(
                text(
                    "INSERT INTO ingestion_errors (run_id, external_id, stage, message, retryable)"
                    " VALUES (:r, :e, :st, :m, :rt)"
                ),
                {"r": run_id, "e": external_id, "st": stage, "m": message, "rt": retryable},
            )

    async def upload_usage(self, ws: WorkspaceId) -> tuple[int, int]:
        async with self._uow() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT count(*), coalesce(sum((d.metadata->>'size')::bigint), 0)"
                        " FROM documents d"
                        " JOIN sources src ON src.id = d.source_id AND src.connector = 'upload'"
                        " WHERE d.workspace_id = :ws AND d.deleted_at IS NULL"
                    ),
                    {"ws": ws},
                )
            ).one()
        return int(row[0]), int(row[1])

    async def create_source(
        self,
        ws: WorkspaceId,
        *,
        connector: str,
        name: str,
        config: dict[str, object],
        visibility: Sequence[str],
        data_class: DataClass,
        created_by: UserId | None = None,
        credential_id: UUID | None = None,
        connection_id: UUID | None = None,
    ) -> SourceId:
        async with self._uow() as s:
            row = (
                await s.execute(
                    text(
                        "INSERT INTO sources (workspace_id, connector, name, config, visibility,"
                        " data_class, created_by, credential_id, connection_id)"
                        " VALUES (:ws, :c, :n, CAST(:cfg AS jsonb), CAST(:v AS text[]), :dc,"
                        " :by, :cred, :conn)"
                        # Any unique rule (the name, one IMAP mailbox per workspace) -> Conflict.
                        " ON CONFLICT DO NOTHING RETURNING id"
                    ),
                    {
                        "ws": ws,
                        "c": connector,
                        "n": name,
                        "cfg": json.dumps(config),
                        "v": list(visibility),
                        "dc": data_class.value,
                        "by": created_by,
                        "cred": credential_id,
                        "conn": connection_id,
                    },
                )
            ).first()
        if row is None:
            raise Conflict(f"{name} is already connected.")
        return SourceId(row[0])

    async def set_credential(
        self, ws: WorkspaceId, source_id: SourceId, credential_id: UUID
    ) -> bool:
        async with self._uow() as s:
            result = await s.execute(
                text("UPDATE sources SET credential_id = :c WHERE id = :id AND workspace_id = :ws"),
                {"c": credential_id, "id": source_id, "ws": ws},
            )
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def update_config(
        self, ws: WorkspaceId, source_id: SourceId, config: dict[str, object]
    ) -> bool:
        async with self._uow() as s:
            result = await s.execute(
                text(
                    "UPDATE sources SET config = CAST(:cfg AS jsonb)"
                    " WHERE id = :id AND workspace_id = :ws"
                ),
                {"cfg": json.dumps(config), "id": source_id, "ws": ws},
            )
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def get_source(self, ws: WorkspaceId, source_id: SourceId) -> SourceRecord | None:
        async with self._uow() as s:
            row = (
                (
                    await s.execute(
                        text(_GET_SOURCE),
                        {"id": source_id, "ws": ws},
                    )
                )
                .mappings()
                .first()
            )
        return None if row is None else _source(row)

    async def sources_by_connector(self, ws: WorkspaceId, connector: str) -> list[SourceRecord]:
        async with self._uow() as s:
            rows = (
                (
                    await s.execute(
                        text(
                            f"SELECT {_SOURCE_COLS} FROM sources"  # noqa: S608
                            " WHERE workspace_id = :ws AND connector = :c ORDER BY name"
                        ),
                        {"ws": ws, "c": connector},
                    )
                )
                .mappings()
                .all()
            )
        return [_source(r) for r in rows]

    async def save_checkpoint(
        self, source_id: SourceId, checkpoint: dict[str, object], *, synced: bool
    ) -> None:
        stamp = ", last_synced_at = now()" if synced else ""
        async with self._uow() as s:
            await s.execute(
                text(f"UPDATE sources SET checkpoint = CAST(:cp AS jsonb){stamp} WHERE id = :id"),  # noqa: S608
                {"id": source_id, "cp": json.dumps(checkpoint)},
            )

    async def due_sources(self, connector: str, limit: int) -> list[tuple[WorkspaceId, SourceId]]:
        async with self._uow() as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT workspace_id, id FROM sources WHERE connector = :c"
                        " AND status = 'active' AND (last_synced_at IS NULL"
                        "  OR last_synced_at + schedule_interval <= now())"
                        " ORDER BY last_synced_at NULLS FIRST LIMIT :n"
                    ),
                    {"c": connector, "n": limit},
                )
            ).all()
        return [(WorkspaceId(r[0]), SourceId(r[1])) for r in rows]

    async def document_versions(
        self, source_id: SourceId, prefix: str
    ) -> dict[str, tuple[str, datetime | None]]:
        async with self._uow() as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT external_id, checksum, source_updated_at FROM documents"
                        " WHERE source_id = :s AND starts_with(external_id, :p)"
                        " AND deleted_at IS NULL"
                    ),
                    {"s": source_id, "p": prefix},
                )
            ).all()
        return {str(r[0]): (str(r[1]), r[2]) for r in rows}

    async def config_in_use(self, connector: str, key: str, value: str) -> bool:
        async with self._uow() as s:
            found = (
                await s.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM sources WHERE connector = :c"
                        " AND config ->> :k = :v AND status <> 'deleting')"
                    ),
                    {"c": connector, "k": key, "v": value},
                )
            ).scalar_one()
        return bool(found)

    async def document_checksums(self, source_id: SourceId, prefix: str) -> dict[str, str]:
        async with self._uow() as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT external_id, checksum FROM documents WHERE source_id = :s"
                        " AND starts_with(external_id, :p) AND deleted_at IS NULL"
                    ),
                    {"s": source_id, "p": prefix},
                )
            ).all()
        return {str(r[0]): str(r[1]) for r in rows}

    async def delete_documents(self, source_id: SourceId, external_ids: Sequence[str]) -> int:
        if not external_ids:
            return 0
        async with self._uow() as s:
            rows = (
                await s.execute(
                    text(
                        "DELETE FROM documents WHERE source_id = :s"
                        " AND external_id = ANY(CAST(:ids AS text[])) RETURNING id"
                    ),
                    {"s": source_id, "ids": list(external_ids)},
                )
            ).all()
        return len(rows)

    async def delete_source(self, ws: WorkspaceId, source_id: SourceId) -> bool:
        async with self._uow() as s:
            row = (
                await s.execute(
                    text(
                        "DELETE FROM sources WHERE id = :id AND workspace_id = :ws"
                        " AND connector <> 'upload' RETURNING id"
                    ),
                    {"id": source_id, "ws": ws},
                )
            ).first()
        return row is not None

    async def blob_in_use(self, blob_key: str) -> bool:
        async with self._uow() as s:
            used = (
                await s.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM documents WHERE metadata->>'blob_key' = :k)"
                        " OR EXISTS (SELECT 1 FROM jobs WHERE payload->>'blob_key' = :k"
                        "   AND status IN ('queued', 'running', 'failed'))"
                    ),
                    {"k": blob_key},
                )
            ).scalar_one()
        return bool(used)
