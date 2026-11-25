"""End to end on the Neon test branch: begin -> real browser-style POST to the bucket ->
complete -> a process_upload job and an ingestion run exist in Postgres.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from core.adapters.postgres.jobs import PostgresJobQueue
from core.adapters.postgres.rate_limit import PostgresRateLimiter
from core.adapters.postgres.sources import PostgresSourceStore
from core.adapters.postgres.uow import UnitOfWork
from core.adapters.s3.blob_store import S3BlobStore, S3Settings
from core.adapters.system import SystemClock
from core.config import Settings
from core.domain.auth import Identity, Role
from core.domain.errors import ValidationFailed
from core.domain.ids import SessionId, UserId, WorkspaceId
from core.domain.principals import Principals
from core.domain.uploads import Visibility
from core.services.uploads import PROCESS_UPLOAD, UploadService, UploadStatus


@pytest.fixture
def blobs(settings: Settings) -> S3BlobStore:
    s = settings
    if not (s.aws_endpoint_url_s3 and s.aws_access_key_id and s.aws_secret_access_key):
        pytest.skip("AWS_* storage settings not configured")
    return S3BlobStore(
        S3Settings(
            endpoint_url=s.aws_endpoint_url_s3,
            region=s.aws_region or "us-east-1",
            access_key_id=s.aws_access_key_id,
            secret_access_key=s.aws_secret_access_key.get_secret_value(),
            bucket=s.uploads_bucket,
        )
    )


@pytest.fixture
async def member(uow: UnitOfWork) -> AsyncIterator[Identity]:
    async with uow() as s:
        ws = (
            await s.execute(
                text(
                    "INSERT INTO workspaces (name, slug)"
                    " VALUES ('up', 'up-' || gen_random_uuid()) RETURNING id"
                )
            )
        ).scalar_one()
        user = (
            await s.execute(
                text(
                    "INSERT INTO users (email, email_canonical, name, password_hash)"
                    " SELECT e, e, 'U', 'h' FROM (SELECT 'up-' || gen_random_uuid() || '@t.dev' AS e) x"
                    " RETURNING id"
                )
            )
        ).scalar_one()
    u, w = UserId(UUID(str(user))), WorkspaceId(UUID(str(ws)))
    yield Identity(
        user_id=u,
        workspace_id=w,
        session_id=SessionId(uuid4()),
        email="u@t.dev",
        name="U",
        role=Role.MEMBER,
        email_verified=True,
        principals=Principals.for_user(u, w),
        csrf_secret="c",
    )
    async with uow() as s:
        await s.execute(text("DELETE FROM workspaces WHERE id = :w"), {"w": ws})
        await s.execute(text("DELETE FROM users WHERE id = :u"), {"u": user})


def service(uow: UnitOfWork, blobs: S3BlobStore) -> UploadService:
    return UploadService(
        blobs,
        PostgresSourceStore(uow),
        PostgresJobQueue(uow),
        PostgresRateLimiter(uow),
        SystemClock(),
        root_secret=b"k" * 32,
    )


async def _post_form(url: str, fields: dict[str, str], data: bytes, name: str) -> int:
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        r = await client.post(url, data=fields, files={"file": (name, data)})
    return r.status_code


async def test_browser_style_upload_queues_processing(
    uow: UnitOfWork, blobs: S3BlobStore, member: Identity
) -> None:
    svc = service(uow, blobs)
    # A unique suffix so repeated runs never collide; hex keeps it valid UTF-8 text.
    data = b"# Handbook\n\nVacation policy: 25 days.\n" + uuid4().hex.encode()
    begun = await svc.begin(
        member,
        filename="handbook.md",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        visibility=Visibility.ONLY_ME,
    )
    assert begun.status is UploadStatus.UPLOAD
    assert begun.upload is not None
    key = begun.upload.fields["key"]
    try:
        assert await _post_form(begun.upload.url, begun.upload.fields, data, "handbook.md") in (
            200,
            204,
        )
        done = await svc.complete(member, begun.upload_token or "")
        assert done.status is UploadStatus.UPLOAD
        assert done.job_id is not None
        job = await PostgresJobQueue(uow).get(done.job_id)
        assert job.kind == PROCESS_UPLOAD
        assert job.payload["acl"] == [f"user:{member.user_id}"]

        # Uploading the same bytes again while it's queued is recognised up front.
        again = await svc.begin(
            member, filename="copy.md", size=len(data),
            sha256=hashlib.sha256(data).hexdigest(), visibility=Visibility.ONLY_ME,
        )  # fmt: skip
        assert again.status is UploadStatus.PROCESSING
    finally:
        await blobs.delete(key)


async def test_bucket_rejects_files_over_the_policy_limit(
    blobs: S3BlobStore, member: Identity
) -> None:
    key = f"tests/{uuid4()}.txt"
    form = await blobs.presigned_upload(key, content_type="text/plain", max_bytes=10)
    status = await _post_form(form.url, form.fields, b"this is longer than ten bytes", "x.txt")
    try:
        assert status >= 400  # enforced by storage, not by our API
        assert await blobs.size_of(key) is None
    finally:
        await blobs.delete(key)


async def test_mislabelled_upload_is_rejected_by_sniffing(
    uow: UnitOfWork, blobs: S3BlobStore, member: Identity
) -> None:
    svc = service(uow, blobs)
    data = b"MZ\x90\x00 definitely not a pdf " + uuid4().bytes
    begun = await svc.begin(
        member, filename="invoice.pdf", size=len(data),
        sha256=hashlib.sha256(data).hexdigest(), visibility=Visibility.ONLY_ME,
    )  # fmt: skip
    assert begun.upload is not None
    try:
        await _post_form(begun.upload.url, begun.upload.fields, data, "invoice.pdf")
        with pytest.raises(ValidationFailed):
            await svc.complete(member, begun.upload_token or "")
    finally:
        await blobs.delete(begun.upload.fields["key"])


@pytest.mark.parametrize("tamper", ["key", "content_type"])
async def test_bucket_rejects_a_tampered_form(blobs: S3BlobStore, tamper: str) -> None:
    key = f"tests/{uuid4()}.txt"
    form = await blobs.presigned_upload(key, content_type="text/plain", max_bytes=1000)
    fields = dict(form.fields)
    other = f"tests/{uuid4()}-someone-else.txt"
    if tamper == "key":
        fields["key"] = other  # try to write somewhere the form wasn't signed for
    else:
        fields["Content-Type"] = "text/html"
    status = await _post_form(form.url, fields, b"hello", "x.txt")
    try:
        assert status >= 400
        assert await blobs.size_of(other) is None
        assert await blobs.size_of(key) is None
    finally:
        await blobs.delete(key)
        await blobs.delete(other)


async def test_bucket_rejects_an_expired_form(blobs: S3BlobStore) -> None:
    key = f"tests/{uuid4()}.txt"
    form = await blobs.presigned_upload(
        key, content_type="text/plain", max_bytes=1000, expires_in=1
    )
    await asyncio.sleep(2.5)
    status = await _post_form(form.url, form.fields, b"hello", "x.txt")
    try:
        assert status >= 400
        assert await blobs.size_of(key) is None
    finally:
        await blobs.delete(key)


async def test_download_links_force_attachment(blobs: S3BlobStore) -> None:
    key = f"tests/{uuid4()}.html"
    await blobs.put(key, b"<script>alert(1)</script>", "text/html")
    try:
        url = await blobs.presigned_url(key, expires_in=60)
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            r = await client.get(url)
        assert r.status_code == 200
        assert r.headers.get("content-disposition", "").startswith("attachment")
        assert "text/html" not in r.headers.get("content-type", "")
    finally:
        await blobs.delete(key)
