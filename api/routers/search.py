"""Search and document preview. Access is enforced in SQL (core/adapters/postgres/search.py);
these handlers only translate HTTP to the service and back."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from api.deps import Container, get_container
from api.schemas.search import (
    DocumentPreviewOut,
    PreviewChunkOut,
    SearchBody,
    SearchHitOut,
    SearchOut,
)
from api.security import CurrentIdentity, MutatingIdentity
from core.domain.documents import SearchFilters
from core.domain.errors import UpstreamUnavailable
from core.domain.ids import DocumentId, SourceId
from core.services.search import SearchService

router = APIRouter(tags=["search"])


def _search(container: Annotated[Container, Depends(get_container)]) -> SearchService:
    if container.search is None:
        raise UpstreamUnavailable("Search isn't configured on this server.")
    return container.search


Search = Annotated[SearchService, Depends(_search)]


@router.post("/search", summary="Hybrid search over what you can see", response_model=SearchOut)
async def search(
    body: SearchBody,
    # POST + CSRF: it reads nothing it shouldn't, but it spends rate and budget (a paid
    # query embedding), and a query in a URL would land in proxy and browser logs. So
    # only verified accounts may search (ADR-0021).
    identity: MutatingIdentity,
    service: Search,
) -> SearchOut:
    res = await service.search(
        identity,
        body.query,
        filters=SearchFilters(
            source_ids=tuple(SourceId(s) for s in body.source_ids),
            updated_after=body.updated_after,
            updated_before=body.updated_before,
        ),
        limit=body.limit,
    )
    return SearchOut(
        hits=[
            SearchHitOut(
                chunk_id=h.chunk_id,
                document_id=h.document_id,
                title=h.chunk.title or "Untitled",
                heading_path=h.chunk.heading_path,
                content=h.chunk.content,
                ordinal=h.chunk.ordinal,
                score=h.score,
                matched_by=sorted(lane.value for lane in h.ranks),
            )
            for h in res.hits
        ],
        lanes=[lane.value for lane in res.lanes],
        notices=list(res.notices),
    )


@router.get(
    "/documents/{document_id}",
    summary="Preview a document you can see (404 for missing and forbidden alike)",
    response_model=DocumentPreviewOut,
)
async def preview(
    document_id: UUID, identity: CurrentIdentity, service: Search
) -> DocumentPreviewOut:
    d = await service.preview(identity, DocumentId(document_id))
    return DocumentPreviewOut(
        id=d.id,
        source_id=d.source_id,
        source_name=d.source_name,
        title=d.title,
        uri=d.uri,
        mime=d.mime,
        source_updated_at=d.source_updated_at,
        chunks=[
            PreviewChunkOut(ordinal=c.ordinal, heading_path=c.heading_path, content=c.content)
            for c in d.chunks
        ],
    )
