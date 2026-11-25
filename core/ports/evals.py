"""Storage for the eval harness: its own workspace and the eval_runs history.

Evals live in a dedicated workspace, so the synthetic corpus never appears in a real
user's search and real documents never leak into eval numbers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import UUID

from core.domain.ids import DocumentId, GenerationId, WorkspaceId


class EvalStore(Protocol):
    async def ensure_workspace(self, slug: str, name: str) -> WorkspaceId: ...

    async def document_ids(
        self, ws: WorkspaceId, external_ids: Sequence[str]
    ) -> dict[str, DocumentId]:
        """Live documents in this workspace by external id (missing ones are absent)."""
        ...

    async def private_document_ids(self, ws: WorkspaceId) -> set[DocumentId]: ...

    async def record_run(
        self,
        *,
        git_sha: str,
        generation: GenerationId | None,
        config: Mapping[str, object],
        metrics: Mapping[str, object],
    ) -> UUID: ...
