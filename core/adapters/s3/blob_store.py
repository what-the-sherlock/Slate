"""BlobStore over an S3-compatible API: Neon Object Storage (ADR-0014).

* Path-style addressing and SigV4: what Neon requires.
* boto3 is synchronous, so each call runs in a worker thread to keep the event
  loop free.
* Explicit connect/read timeouts, and botocore's "standard" retry mode (exponential
  backoff with jitter, honours throttling responses). Every call here is idempotent:
  a PUT to the same key overwrites, a DELETE of a missing key succeeds.
* Vendor errors become domain errors at this edge.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from core.domain.errors import NotFound, UpstreamUnavailable

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

MAX_PRESIGN_SECONDS = 3600  # presigned links stay short-lived (SECURITY.md)


@dataclass(frozen=True, slots=True)
class S3Settings:
    endpoint_url: str
    region: str
    access_key_id: str
    secret_access_key: str
    bucket: str


class S3BlobStore:
    def __init__(self, settings: S3Settings, *, client: S3Client | None = None) -> None:
        self._bucket = settings.bucket
        self._client: S3Client = client or boto3.client(  # pyright: ignore[reportUnknownMemberType]
            "s3",
            endpoint_url=settings.endpoint_url,
            region_name=settings.region,
            aws_access_key_id=settings.access_key_id,
            aws_secret_access_key=settings.secret_access_key,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=5,
                read_timeout=30,
                retries={"mode": "standard", "max_attempts": 4},
            ),
        )

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        await self._call(
            lambda: self._client.put_object(
                Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
            )
        )
        return key

    async def get(self, ref: str) -> bytes:
        def read() -> bytes:
            obj = self._client.get_object(Bucket=self._bucket, Key=ref)
            return obj["Body"].read()

        return await self._call(read, missing=ref)

    async def delete(self, ref: str) -> None:
        await self._call(lambda: self._client.delete_object(Bucket=self._bucket, Key=ref))

    async def presigned_url(self, ref: str, *, expires_in: int = 300) -> str:
        expires = max(1, min(expires_in, MAX_PRESIGN_SECONDS))
        return await self._call(
            lambda: self._client.generate_presigned_url(
                "get_object", Params={"Bucket": self._bucket, "Key": ref}, ExpiresIn=expires
            )
        )

    async def _call[T](self, fn: Callable[[], T], *, missing: str | None = None) -> T:
        try:
            return await asyncio.to_thread(fn)
        except ClientError as e:
            code = str(e.response.get("Error", {}).get("Code", ""))
            if code in {"NoSuchKey", "404", "NotFound"} and missing is not None:
                raise NotFound("blob not found") from e
            raise UpstreamUnavailable(f"object storage error ({code or 'unknown'})") from e
        except BotoCoreError as e:  # timeouts, connection errors after retries
            raise UpstreamUnavailable(f"object storage unreachable ({type(e).__name__})") from e
