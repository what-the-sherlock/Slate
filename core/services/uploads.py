"""Direct-to-storage uploads in two steps (ADR-0016; security-reviewed 2026-11-24).

1. ``begin``: check permission, quota, size, type and duplicates; hand back a presigned
   POST (the bucket itself rejects anything over the limit) plus a signed upload token.
2. ``complete``: verify the token, confirm the object's size and its real type from the
   first bytes, then queue ``process_upload``.

The checks in ``complete`` are a fast early warning, NOT the security boundary: the
presigned form stays valid for a few minutes, so the object could be replaced afterwards.
The worker is the gate. It downloads once (byte-capped), and hashes, sniffs and parses
those same bytes, failing the job if the SHA-256 differs from the one announced here.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from core.domain.auth import Identity, Role
from core.domain.errors import Conflict, PermissionDenied, RateLimited, ValidationFailed
from core.domain.ids import DocumentId, JobId, WorkspaceId
from core.domain.jobs import JobStatus
from core.domain.signed_tokens import sign, verify
from core.domain.uploads import (
    ALLOWED_DESCRIPTION,
    MAX_DOCUMENTS_PER_WORKSPACE,
    MAX_STORED_BYTES_PER_WORKSPACE,
    MAX_UPLOAD_BYTES,
    MIME,
    SNIFF_BYTES,
    FileKind,
    Visibility,
    access_for,
    blob_key_for,
    external_id_for,
    is_sha256,
    kind_from_filename,
    safe_filename,
    sniff_kind,
)
from core.ports.jobs import JobQueue
from core.ports.rate_limit import Limit, RateLimiter
from core.ports.sources import SourceStore
from core.ports.storage import BlobStore, PresignedUpload
from core.ports.system import Clock
from core.services.mail import IMPORT_MBOX, mbox_source

PROCESS_UPLOAD = "process_upload"
TOKEN_PURPOSE = "upload"  # noqa: S105 (token purpose label, not a secret)
TOKEN_TTL = timedelta(minutes=15)
UPLOAD_URL_TTL_S = 600
_IN_FLIGHT = {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.FAILED}

# Abuse limits per uploader (security review: storage could otherwise be filled freely).
BEGIN_LIMIT = Limit(budget=30, window=timedelta(hours=1))
BYTES_LIMIT = Limit(budget=500 * 1024 * 1024, window=timedelta(days=1))


class UploadStatus(StrEnum):
    UPLOAD = "upload"  # send the bytes, then call complete / processing was queued
    DUPLICATE = "duplicate"  # already indexed with the same access: nothing to do
    PROCESSING = "processing"  # an identical upload is already queued
    VISIBILITY_DIFFERS = "visibility_differs"  # indexed, but shared differently


@dataclass(frozen=True, slots=True)
class BeginResult:
    status: UploadStatus
    filename: str
    document_id: DocumentId | None = None
    upload: PresignedUpload | None = None
    upload_token: str | None = None


@dataclass(frozen=True, slots=True)
class CompleteResult:
    status: UploadStatus
    job_id: JobId | None = None
    document_id: DocumentId | None = None
    size: int = 0
    inline: bool = True  # may the API process it right away (an .mbox goes to the worker)


def derive_key(root_secret: bytes, purpose: str) -> bytes:
    """A separate key per use of the root secret, so a MAC made for one purpose can never
    validate in another (key separation).
    """
    return hmac.new(root_secret, f"slate/{purpose}/v1".encode(), hashlib.sha256).digest()


class UploadService:
    def __init__(
        self,
        blobs: BlobStore,
        sources: SourceStore,
        jobs: JobQueue,
        limiter: RateLimiter,
        clock: Clock,
        *,
        root_secret: bytes,
    ) -> None:
        if len(root_secret) < 32:
            raise ValueError("root secret must be at least 32 bytes")
        self._blobs = blobs
        self._sources = sources
        self._jobs = jobs
        self._limiter = limiter
        self._clock = clock
        self._secret = derive_key(root_secret, "upload-token")

    async def begin(
        self,
        identity: Identity,
        *,
        filename: str,
        size: int,
        sha256: str,
        visibility: Visibility,
    ) -> BeginResult:
        self._require_uploader(identity)
        name = safe_filename(filename)
        kind = kind_from_filename(name)
        errors: dict[str, str] = {}
        if kind is None:
            errors["file"] = f"This file type isn't supported. Upload {ALLOWED_DESCRIPTION}."
        elif size <= 0:
            errors["file"] = "This file is empty."
        elif size > MAX_UPLOAD_BYTES:
            errors["file"] = "This file is larger than 25 MB."
        if not is_sha256(sha256):
            errors["sha256"] = "Invalid file checksum."
        if errors or kind is None:
            raise ValidationFailed("This file can't be uploaded.", extra={"fields": errors})

        existing = await self._existing(identity, sha256, visibility)
        if existing is not None:
            return BeginResult(existing[0], name, document_id=existing[1])
        if await self._in_flight(identity.workspace_id, sha256, identity):
            return BeginResult(UploadStatus.PROCESSING, name)

        await self._check_workspace_quota(identity.workspace_id, size)
        who = f"upload:{identity.user_id}"
        if not await self._limiter.try_spend(f"{who}:begin", 1, BEGIN_LIMIT):
            raise RateLimited(
                "You've started a lot of uploads. Try again in an hour.", retry_after_s=3600
            )
        if not await self._limiter.try_spend(f"{who}:bytes", size, BYTES_LIMIT):
            raise RateLimited("You've reached today's upload limit (500 MB).", retry_after_s=86400)

        key = blob_key_for(identity.workspace_id, identity.user_id, sha256, kind)
        upload = await self._blobs.presigned_upload(
            key, content_type=MIME[kind], max_bytes=MAX_UPLOAD_BYTES, expires_in=UPLOAD_URL_TTL_S
        )
        token = sign(
            {
                "u": str(identity.user_id),
                "w": str(identity.workspace_id),
                "k": key,
                "n": name,
                "t": kind.value,
                "s": size,
                "h": sha256,
                "v": visibility.value,
            },
            secret=self._secret,
            purpose=TOKEN_PURPOSE,
            expires_at=self._clock.now() + TOKEN_TTL,
        )
        return BeginResult(UploadStatus.UPLOAD, name, upload=upload, upload_token=token)

    async def complete(self, identity: Identity, upload_token: str) -> CompleteResult:
        claim = verify(
            upload_token, secret=self._secret, purpose=TOKEN_PURPOSE, now=self._clock.now()
        )
        if claim.get("u") != str(identity.user_id) or claim.get("w") != str(identity.workspace_id):
            raise PermissionDenied("This upload belongs to someone else.")
        try:
            visibility = Visibility(str(claim["v"]))
        except ValueError:
            # e.g. a token issued for the removed "public" choice before ADR-0026 shipped
            raise ValidationFailed("This upload link has expired. Upload the file again.") from None
        self._require_uploader(identity)
        key, kind, size = str(claim["k"]), FileKind(str(claim["t"])), int(str(claim["s"]))
        sha256, name = str(claim["h"]), str(claim["n"])

        existing = await self._existing(identity, sha256, visibility)
        if existing is not None:
            return CompleteResult(existing[0], document_id=existing[1])

        actual = await self._blobs.size_of(key)
        if actual is None:
            raise Conflict("The file hasn't finished uploading. Try again.")
        if actual != size:
            raise ValidationFailed("The uploaded file doesn't match what was announced.")
        head = await self._blobs.read_head(key, SNIFF_BYTES)
        if sniff_kind(head, kind) is None:
            raise ValidationFailed(
                f"This doesn't look like a valid .{kind.value} file.",
                extra={"fields": {"file": f"The contents don't match a .{kind.value} file."}},
            )

        if kind is FileKind.MBOX:
            return await self._enqueue_mbox(identity, key, sha256, size, name)
        access = access_for(visibility, identity.user_id, identity.workspace_id)
        source = await self._sources.upload_source(identity.workspace_id, access.data_class)
        external_id = external_id_for(sha256, identity.user_id)
        job_key = _job_key(identity.workspace_id, external_id)
        prior = await self._jobs.find_by_key(job_key)
        if prior is not None and prior.status in _IN_FLIGHT:
            return CompleteResult(UploadStatus.PROCESSING, job_id=prior.id)
        if prior is not None and prior.status is JobStatus.DEAD:
            # Deterministic, so concurrent retries of the same dead job collapse into one.
            job_key = f"{job_key}:retry-of:{prior.id}"
        job = await self._jobs.enqueue(
            identity.workspace_id,
            PROCESS_UPLOAD,
            {
                "blob_key": key,
                "filename": name,
                "kind": kind.value,
                "mime": MIME[kind],
                "size": size,
                "sha256": sha256,
                "acl": list(access.acl),
                "data_class": access.data_class.value,
                "source_id": str(source),
                "external_id": external_id,
                "uploader": str(identity.user_id),
            },
            job_key,
        )
        return CompleteResult(UploadStatus.UPLOAD, job_id=job, size=size)

    async def _existing(
        self, identity: Identity, sha256: str, visibility: Visibility
    ) -> tuple[UploadStatus, DocumentId] | None:
        """An indexed copy: DUPLICATE if shared the same way, else VISIBILITY_DIFFERS.

        Never report a differently-shared copy as a plain duplicate: a user narrowing
        access by re-uploading must not be told it worked while it stays visible.
        """
        found = await self._sources.find_upload(
            identity.workspace_id, external_id_for(sha256, identity.user_id)
        )
        if found is None:
            return None
        wanted = access_for(visibility, identity.user_id, identity.workspace_id)
        same = set(found.acl) == set(wanted.acl) and found.data_class is wanted.data_class
        status = UploadStatus.DUPLICATE if same else UploadStatus.VISIBILITY_DIFFERS
        return status, found.document_id

    async def _in_flight(self, ws: WorkspaceId, sha256: str, identity: Identity) -> bool:
        job = await self._jobs.find_by_key(_job_key(ws, external_id_for(sha256, identity.user_id)))
        return job is not None and job.status in _IN_FLIGHT

    async def _check_workspace_quota(self, ws: WorkspaceId, size: int) -> None:
        """Storage per workspace (ADR-0021). Re-uploads of a known file returned earlier."""
        documents, stored = await self._sources.upload_usage(ws)
        if documents >= MAX_DOCUMENTS_PER_WORKSPACE:
            message = (
                f"Your workspace has reached its limit of {MAX_DOCUMENTS_PER_WORKSPACE} documents."
            )
            raise ValidationFailed(message, extra={"fields": {"file": message}})
        if stored + size > MAX_STORED_BYTES_PER_WORKSPACE:
            limit_mb = MAX_STORED_BYTES_PER_WORKSPACE // (1024 * 1024)
            message = f"This file would take your workspace over its {limit_mb} MB storage limit."
            raise ValidationFailed(message, extra={"fields": {"file": message}})

    async def _enqueue_mbox(
        self, identity: Identity, key: str, sha256: str, size: int, name: str
    ) -> CompleteResult:
        """Email exports become thread documents in the user's own "Email import" source,
        always private to them (ADR-0028), whatever visibility was asked for."""
        source = await mbox_source(self._sources, identity)
        job_key = f"{IMPORT_MBOX}:{identity.workspace_id}:{identity.user_id}:{sha256}"
        prior = await self._jobs.find_by_key(job_key)
        if prior is not None and prior.status in _IN_FLIGHT:
            return CompleteResult(UploadStatus.PROCESSING, job_id=prior.id, inline=False)
        if prior is not None and prior.status in (JobStatus.DEAD, JobStatus.SUCCEEDED):
            job_key = f"{job_key}:again:{prior.id}"  # importing the same file again re-runs it
        job = await self._jobs.enqueue(
            identity.workspace_id,
            IMPORT_MBOX,
            {"blob_key": key, "sha256": sha256, "size": size, "filename": name,
             "source_id": str(source), "uploader": str(identity.user_id)},
            job_key,
        )  # fmt: skip
        return CompleteResult(UploadStatus.UPLOAD, job_id=job, size=size, inline=False)

    @staticmethod
    def _require_uploader(identity: Identity) -> None:
        if not identity.can(Role.MEMBER):
            raise PermissionDenied("Your role can't upload files.")


def _job_key(ws: WorkspaceId, external_id: str) -> str:
    # Per workspace, not per source: one key covers both upload sources.
    return f"{PROCESS_UPLOAD}:{ws}:{external_id}"
