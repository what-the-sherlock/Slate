from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from api.deps import Container, get_container
from api.schemas.uploads import (
    BeginUploadBody,
    BeginUploadResponse,
    CompleteUploadBody,
    CompleteUploadResponse,
    PresignedPost,
    UploadStatusName,
)
from api.security import MutatingIdentity
from core.domain.errors import UpstreamUnavailable
from core.domain.uploads import Visibility
from core.services.inline import INLINE_MAX_BYTES, InlineOutcome
from core.services.uploads import UploadService, UploadStatus

router = APIRouter(tags=["uploads"])


def _uploads(container: Annotated[Container, Depends(get_container)]) -> UploadService:
    if container.uploads is None:
        raise UpstreamUnavailable("File storage isn't configured on this server.")
    return container.uploads


Uploads = Annotated[UploadService, Depends(_uploads)]


@router.post(
    "/uploads",
    summary="Start an upload: returns a direct-to-storage form, or says it's a duplicate",
    response_model=BeginUploadResponse,
)
async def begin_upload(
    body: BeginUploadBody, identity: MutatingIdentity, uploads: Uploads
) -> BeginUploadResponse:
    result = await uploads.begin(
        identity,
        filename=body.filename,
        size=body.size,
        sha256=body.sha256.lower(),
        visibility=Visibility(body.visibility),
    )
    return BeginUploadResponse(
        status=result.status.value,
        filename=result.filename,
        document_id=result.document_id,
        upload=PresignedPost(url=result.upload.url, fields=result.upload.fields)
        if result.upload
        else None,
        upload_token=result.upload_token,
    )


@router.post(
    "/uploads/complete",
    summary="Finish an upload after the file reached storage: checks it and queues processing",
    response_model=CompleteUploadResponse,
)
async def complete_upload(
    body: CompleteUploadBody,
    identity: MutatingIdentity,
    uploads: Uploads,
    container: Annotated[Container, Depends(get_container)],
) -> CompleteUploadResponse:
    result = await uploads.complete(identity, body.upload_token)
    status: UploadStatusName = result.status.value
    # Small files are processed right now, so they're searchable in this response;
    # bigger ones (or a busy/unavailable pipeline) are left for the worker.
    run_inline = (
        result.status is UploadStatus.UPLOAD
        and result.job_id is not None
        and result.inline
        and container.inline is not None
        and result.size <= INLINE_MAX_BYTES
    )
    if run_inline and container.inline and result.job_id:
        outcome = await container.inline.run(result.job_id)
        if outcome is InlineOutcome.DONE:
            status = "indexed"
    return CompleteUploadResponse(
        status=status,
        job_id=result.job_id,
        document_id=result.document_id,
    )
