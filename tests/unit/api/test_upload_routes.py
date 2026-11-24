import hashlib

from fastapi.testclient import TestClient

from api.security import CSRF_HEADER
from tests.fakes.storage import InMemoryBlobStore

SAME_SITE = {"x-requested-with": "slate"}
SETUP = {
    "workspace_name": "Acme",
    "name": "Ada",
    "email": "ada@acme.dev",
    "password": "correct horse battery",
}
DATA = b"# Notes\n\nhello"


def owner(client: TestClient) -> dict[str, str]:
    assert client.post("/api/v1/auth/setup", json=SETUP, headers=SAME_SITE).status_code == 201
    return {CSRF_HEADER: client.cookies.get("slate_csrf") or ""}


def begin_body(name: str = "notes.md") -> dict[str, object]:
    return {"filename": name, "size": len(DATA), "sha256": hashlib.sha256(DATA).hexdigest()}


def test_upload_flow_over_http(client: TestClient, blobs: InMemoryBlobStore) -> None:
    csrf = owner(client)
    r = client.post("/api/v1/uploads", json=begin_body(), headers=csrf)
    assert r.status_code == 200, r.text
    begun = r.json()
    assert begun["status"] == "upload"
    assert begun["upload"]["fields"]["key"].endswith(".md")
    blobs.blobs[begun["upload"]["fields"]["key"]] = (DATA, "text/markdown")  # the browser's POST
    r = client.post(
        "/api/v1/uploads/complete", json={"upload_token": begun["upload_token"]}, headers=csrf
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "upload"
    assert r.json()["job_id"]


def test_upload_requires_csrf(client: TestClient) -> None:
    owner(client)
    assert client.post("/api/v1/uploads", json=begin_body()).status_code == 403


def test_guest_cannot_upload(client: TestClient) -> None:
    owner(client)
    client.cookies.clear()
    client.post("/api/v1/auth/guest", headers=SAME_SITE)
    csrf = {CSRF_HEADER: client.cookies.get("slate_csrf") or ""}
    assert client.post("/api/v1/uploads", json=begin_body(), headers=csrf).status_code == 403


def test_unsupported_type_is_a_field_error(client: TestClient) -> None:
    csrf = owner(client)
    r = client.post("/api/v1/uploads", json=begin_body("setup.exe"), headers=csrf)
    assert r.status_code == 422
    assert "file" in r.json()["fields"]


def test_forged_token_is_rejected(client: TestClient) -> None:
    csrf = owner(client)
    r = client.post(
        "/api/v1/uploads/complete", json={"upload_token": "forged.token-value"}, headers=csrf
    )
    assert r.status_code == 422
