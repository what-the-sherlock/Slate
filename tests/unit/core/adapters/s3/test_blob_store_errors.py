"""Vendor errors become domain errors, without touching the network."""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError

from core.adapters.s3.blob_store import MAX_PRESIGN_SECONDS, S3BlobStore, S3Settings
from core.domain.errors import NotFound, UpstreamUnavailable

SETTINGS = S3Settings("https://s3.test", "us-east-1", "id", "secret", "uploads")


class Stub:
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.presign_args: dict[str, Any] = {}

    def get_object(self, **_: Any) -> Any:
        assert self.exc is not None
        raise self.exc

    put_object = delete_object = get_object

    def generate_presigned_url(self, op: str, Params: dict[str, Any], ExpiresIn: int) -> str:  # noqa: N803
        self.presign_args = {"op": op, "expires": ExpiresIn, **Params}
        return "https://signed"


def store(stub: Stub) -> S3BlobStore:
    return S3BlobStore(SETTINGS, client=stub)  # type: ignore[arg-type]


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "GetObject")


async def test_missing_key_is_not_found() -> None:
    with pytest.raises(NotFound):
        await store(Stub(client_error("NoSuchKey"))).get("k")


async def test_other_client_errors_are_upstream_unavailable() -> None:
    with pytest.raises(UpstreamUnavailable, match="AccessDenied"):
        await store(Stub(client_error("AccessDenied"))).put("k", b"x", "text/plain")


async def test_timeouts_are_upstream_unavailable() -> None:
    with pytest.raises(UpstreamUnavailable, match="ConnectTimeoutError"):
        await store(Stub(ConnectTimeoutError(endpoint_url="https://s3.test"))).delete("k")


async def test_presigned_urls_are_short_lived() -> None:
    stub = Stub()
    await store(stub).presigned_url("k", expires_in=10**6)
    assert stub.presign_args["expires"] == MAX_PRESIGN_SECONDS
    assert stub.presign_args["Bucket"] == "uploads"
