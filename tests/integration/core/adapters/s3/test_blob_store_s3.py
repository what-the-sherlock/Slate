"""S3BlobStore against the Neon test branch's `uploads` bucket."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from core.adapters.s3.blob_store import S3BlobStore, S3Settings
from core.config import Settings
from core.domain.errors import NotFound


@pytest.fixture
def store(settings: Settings) -> S3BlobStore:
    if not (
        settings.aws_endpoint_url_s3
        and settings.aws_access_key_id
        and settings.aws_secret_access_key
    ):
        pytest.skip(
            "AWS_* storage settings not configured (run `neon env pull` for the test branch)"
        )
    return S3BlobStore(
        S3Settings(
            endpoint_url=settings.aws_endpoint_url_s3,
            region=settings.aws_region or "us-east-1",
            access_key_id=settings.aws_access_key_id,
            secret_access_key=settings.aws_secret_access_key.get_secret_value(),
            bucket=settings.uploads_bucket,
        )
    )


async def test_put_get_presign_delete_roundtrip(store: S3BlobStore) -> None:
    key = f"tests/{uuid4()}.txt"
    data = b"Hello from the Slate integration tests."
    try:
        assert await store.put(key, data, "text/plain") == key
        assert await store.get(key) == data

        url = await store.presigned_url(key, expires_in=60)
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            r = await client.get(url)
        assert r.status_code == 200
        assert r.content == data
    finally:
        await store.delete(key)
    with pytest.raises(NotFound):
        await store.get(key)
    await store.delete(key)  # deleting a missing blob is fine (idempotent)
