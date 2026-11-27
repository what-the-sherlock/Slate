import hashlib

from fastapi.testclient import TestClient

from tests.fakes.auth import FakeMailer
from tests.fakes.storage import InMemoryBlobStore
from tests.fakes.system import FakeWorkerTrigger
from tests.unit.api.accounts import sign_up

DATA = b"# Notes\n\nhello"


def owner(client: TestClient, mailer: FakeMailer) -> dict[str, str]:
    return sign_up(client, mailer)


def begin_body(name: str = "notes.md") -> dict[str, object]:
    return {"filename": name, "size": len(DATA), "sha256": hashlib.sha256(DATA).hexdigest()}


def test_upload_flow_over_http(
    client: TestClient,
    mailer: FakeMailer,
    blobs: InMemoryBlobStore,
    worker_trigger: FakeWorkerTrigger,
) -> None:
    csrf = owner(client, mailer)
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
    assert worker_trigger.reasons == ["upload"]  # queued for the worker: start it now


def test_upload_requires_csrf(client: TestClient, mailer: FakeMailer) -> None:
    owner(client, mailer)
    assert client.post("/api/v1/uploads", json=begin_body()).status_code == 403


def test_unverified_accounts_cannot_upload(client: TestClient, mailer: FakeMailer) -> None:
    csrf = sign_up(client, mailer, verify=False)
    r = client.post("/api/v1/uploads", json=begin_body(), headers=csrf)
    assert r.status_code == 403
    assert r.json()["reason"] == "email_unverified"


def test_unsupported_type_is_a_field_error(client: TestClient, mailer: FakeMailer) -> None:
    csrf = owner(client, mailer)
    r = client.post("/api/v1/uploads", json=begin_body("setup.exe"), headers=csrf)
    assert r.status_code == 422
    assert "file" in r.json()["fields"]


def test_forged_token_is_rejected(client: TestClient, mailer: FakeMailer) -> None:
    csrf = owner(client, mailer)
    r = client.post(
        "/api/v1/uploads/complete", json={"upload_token": "forged.token-value"}, headers=csrf
    )
    assert r.status_code == 422
