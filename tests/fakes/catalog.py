from __future__ import annotations

from core.domain.errors import NotFound
from core.domain.ids import JobId, SourceId, WorkspaceId
from core.domain.principals import Principals
from core.ports.catalog import JobView, RunSummary, SourceSummary


class InMemoryCatalog:
    def __init__(self) -> None:
        self.source_rows: list[tuple[WorkspaceId, SourceSummary]] = []
        self.run_rows: dict[SourceId, list[RunSummary]] = {}
        self.jobs: dict[JobId, tuple[WorkspaceId, JobView]] = {}

    async def sources(self, ws: WorkspaceId, principals: Principals) -> list[SourceSummary]:
        allowed = set(principals.values)
        return [s for w, s in self.source_rows if w == ws and allowed & set(s.visibility)]

    async def runs(
        self, ws: WorkspaceId, principals: Principals, source_id: SourceId, limit: int
    ) -> list[RunSummary]:
        if not any(s.id == source_id for s in await self.sources(ws, principals)):
            raise NotFound("source not found")
        return self.run_rows.get(source_id, [])[:limit]

    async def job(self, ws: WorkspaceId, job_id: JobId) -> JobView:
        found = self.jobs.get(job_id)
        if found is None or found[0] != ws:
            raise NotFound("job not found")
        return found[1]
