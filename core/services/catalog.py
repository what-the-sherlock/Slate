"""Sources page use cases (read-only, permission-aware).

Shared upload sources hold documents with *per-document* ACLs, but runs, errors and
pending jobs belong to whoever uploaded. So on upload sources only admins see run
history, errors and pending counts (security review); everyone else sees the documents
they're allowed to see, and tracks their own uploads through ``job``.
"""

from __future__ import annotations

from dataclasses import replace

from core.domain.auth import Identity, Role
from core.domain.errors import NotFound
from core.domain.ids import JobId, SourceId
from core.ports.catalog import Catalog, JobView, RunSummary, SourceSummary

PER_UPLOADER_CONNECTORS = {"upload"}


class CatalogService:
    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog

    async def sources(self, identity: Identity) -> list[SourceSummary]:
        rows = await self._catalog.sources(identity.workspace_id, identity.principals)
        if identity.can(Role.ADMIN):
            return rows
        return [self._redact(s) for s in rows]

    async def runs(
        self, identity: Identity, source_id: SourceId, limit: int = 20
    ) -> list[RunSummary]:
        sources = {
            s.id: s for s in await self._catalog.sources(identity.workspace_id, identity.principals)
        }
        source = sources.get(source_id)
        if source is None:
            raise NotFound("source not found")
        if source.connector in PER_UPLOADER_CONNECTORS and not identity.can(Role.ADMIN):
            return []
        return await self._catalog.runs(
            identity.workspace_id, identity.principals, source_id, max(1, min(limit, 100))
        )

    async def job(self, identity: Identity, job_id: JobId) -> JobView:
        """Uploaders see their own jobs; admins see all. Others get NotFound (no probing)."""
        job = await self._catalog.job(identity.workspace_id, job_id)
        if job.uploader != identity.user_id and not identity.can(Role.ADMIN):
            raise NotFound("job not found")
        return job

    @staticmethod
    def _redact(s: SourceSummary) -> SourceSummary:
        if s.connector not in PER_UPLOADER_CONNECTORS:
            return s
        return replace(s, pending_jobs=0, last_run_status=None, last_run_error=None)
