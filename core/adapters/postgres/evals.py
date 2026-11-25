"""EvalStore over Postgres."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from uuid import UUID

from sqlalchemy import text

from core.adapters.postgres.uow import UnitOfWork
from core.domain.ids import DocumentId, GenerationId, WorkspaceId


class PostgresEvalStore:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def ensure_workspace(self, slug: str, name: str) -> WorkspaceId:
        async with self._uow() as s:
            await s.execute(
                text(
                    "INSERT INTO workspaces (name, slug) VALUES (:n, :s)"
                    " ON CONFLICT (slug) DO NOTHING"
                ),
                {"n": name, "s": slug},
            )
            ws = (
                await s.execute(text("SELECT id FROM workspaces WHERE slug = :s"), {"s": slug})
            ).scalar_one()
        return WorkspaceId(UUID(str(ws)))

    async def document_ids(
        self, ws: WorkspaceId, external_ids: Sequence[str]
    ) -> dict[str, DocumentId]:
        async with self._uow() as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT external_id, id FROM documents WHERE workspace_id = :ws"
                        " AND deleted_at IS NULL AND external_id = ANY(CAST(:e AS text[]))"
                    ),
                    {"ws": ws, "e": list(external_ids)},
                )
            ).all()
        return {str(r[0]): DocumentId(r[1]) for r in rows}

    async def private_document_ids(self, ws: WorkspaceId) -> set[DocumentId]:
        async with self._uow() as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT id FROM documents WHERE workspace_id = :ws"
                        " AND deleted_at IS NULL AND NOT ('public' = ANY(acl))"
                    ),
                    {"ws": ws},
                )
            ).scalars()
            return {DocumentId(r) for r in rows}

    async def record_run(
        self,
        *,
        git_sha: str,
        generation: GenerationId | None,
        config: Mapping[str, object],
        metrics: Mapping[str, object],
    ) -> UUID:
        async with self._uow() as s:
            run = (
                await s.execute(
                    text(
                        "INSERT INTO eval_runs (git_sha, generation_id, config, metrics)"
                        " VALUES (:sha, :g, CAST(:c AS jsonb), CAST(:m AS jsonb)) RETURNING id"
                    ),
                    {
                        "sha": git_sha,
                        "g": generation,
                        "c": json.dumps(config),
                        "m": json.dumps(metrics),
                    },
                )
            ).scalar_one()
        return UUID(str(run))
