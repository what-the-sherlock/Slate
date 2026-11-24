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
)
from api.security import MutatingIdentity
from core.domain.errors import UpstreamUnavailable
from core.domain.uploads import Visibility
from core.services.uploads import UploadService

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
    body: CompleteUploadBody, identity: MutatingIdentity, uploads: Uploads
) -> CompleteUploadResponse:
    result = await uploads.complete(identity, body.upload_token)
    return CompleteUploadResponse(
        status=result.status.value, job_id=result.job_id, document_id=result.document_id
    )
