import pytest
from fastapi import FastAPI, Query
from fastapi.testclient import TestClient

from core.domain.errors import AppError, NotFound, PermissionDenied, RateLimited


@pytest.fixture
def raising_client(app: FastAPI) -> TestClient:
    @app.get("/boom/{kind}")
    async def boom(kind: str, n: int = Query(default=1, ge=1)) -> dict[str, int]:
        errors: dict[str, AppError | Exception] = {
            "notfound": NotFound("doc 123 not found"),
            "forbidden": PermissionDenied("nope"),
            "ratelimited": RateLimited("slow down", retry_after_s=20),
            "crash": RuntimeError("db password is hunter2"),
        }
        if kind in errors:
            raise errors[kind]
        return {"n": n}

    return TestClient(app, raise_server_exceptions=False)


def test_app_error_becomes_problem_json(raising_client: TestClient) -> None:
    r = raising_client.get("/boom/notfound")
    assert r.status_code == 404
    assert r.headers["content-type"] == "application/problem+json"
    body = r.json()
    assert body["type"].endswith("/not-found")
    assert body["title"] == "Not found"
    assert body["detail"] == "doc 123 not found"
    assert body["instance"] == "/boom/notfound"
    assert body["request_id"] == r.headers["x-request-id"]


def test_status_mapping(raising_client: TestClient) -> None:
    assert raising_client.get("/boom/forbidden").status_code == 403


def test_rate_limited_sets_retry_after(raising_client: TestClient) -> None:
    r = raising_client.get("/boom/ratelimited")
    assert r.status_code == 429
    assert r.headers["retry-after"] == "20"
    assert r.json()["retry_after_s"] == 20


def test_unexpected_error_hides_internals(raising_client: TestClient) -> None:
    r = raising_client.get("/boom/crash")
    assert r.status_code == 500
    assert "hunter2" not in r.text
    assert r.json()["type"].endswith("/internal")
    assert r.json()["request_id"]


def test_validation_error_is_problem_json(raising_client: TestClient) -> None:
    r = raising_client.get("/boom/ok?n=0")
    assert r.status_code == 422
    assert r.headers["content-type"] == "application/problem+json"
    assert r.json()["errors"][0]["loc"] == ["query", "n"]


def test_unknown_route_is_problem_json(client: TestClient) -> None:
    r = client.get("/api/v1/nope")
    assert r.status_code == 404
    assert r.headers["content-type"] == "application/problem+json"


def test_request_id_is_generated_and_echoed(client: TestClient) -> None:
    r = client.get("/api/v1/healthz")
    assert r.headers["x-request-id"].startswith("req_")


def test_well_formed_incoming_request_id_is_kept(client: TestClient) -> None:
    r = client.get("/api/v1/healthz", headers={"x-request-id": "trace-abc12345"})
    assert r.headers["x-request-id"] == "trace-abc12345"


def test_malformed_incoming_request_id_is_replaced(client: TestClient) -> None:
    r = client.get("/api/v1/healthz", headers={"x-request-id": "bad id\nwith newline"})
    assert r.headers["x-request-id"].startswith("req_")
