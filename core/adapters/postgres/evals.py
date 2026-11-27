"""EvalStore over Postgres."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from uuid import UUID

from sqlalchemy import text

from core.adapters.postgres.uow import UnitOfWork
from core.domain.ids import DocumentId, GenerationId, SourceId, WorkspaceId


class PostgresEvalStore:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def ensure_workspace(self, slug: str, name: str) -> WorkspaceId:
        async with self._uow() as s:
            await s.execute(
                text(
                    # The only workspace allowed public data (ADR-0026, migration 0006).
                    "INSERT INTO workspaces (name, slug, allows_public) VALUES (:n, :s, true)"
                    " ON CONFLICT (slug) DO UPDATE SET allows_public = true"
                ),
                {"n": name, "s": slug},
            )
            ws = (
                await s.execute(text("SELECT id FROM workspaces WHERE slug = :s"), {"s": slug})
            ).scalar_one()
        return WorkspaceId(UUID(str(ws)))

    async def ensure_source(self, ws: WorkspaceId, connector: str, name: str) -> SourceId:
        """A private, workspace-visible source imitating ``connector`` in the eval workspace
        (ADR-0031), so source-kind filters can be evaluated. Idempotent by name. Refuses
        a workspace that isn't the eval one, so it can never add a source to a real one."""
        async with self._uow() as s:
            allowed = (
                await s.execute(
                    text("SELECT allows_public FROM workspaces WHERE id = :ws"), {"ws": ws}
                )
            ).scalar_one_or_none()
            if allowed is not True:
                raise RuntimeError("ensure_source is for the eval workspace only")
            await s.execute(
                text(
                    "INSERT INTO sources (workspace_id, connector, name, visibility, data_class)"
                    " VALUES (:ws, :c, :n, CAST(:v AS text[]), 'private')"
                    " ON CONFLICT (workspace_id, name) DO NOTHING"
                ),
                {"ws": ws, "c": connector, "n": name, "v": [f"workspace:{ws}"]},
            )
            row = (
                await s.execute(
                    text(
                        "SELECT id, connector FROM sources WHERE workspace_id = :ws AND name = :n"
                    ),
                    {"ws": ws, "n": name},
                )
            ).one()
        if row[1] != connector:
            raise RuntimeError(f"source {name!r} exists with another connector")
        return SourceId(row[0])

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
