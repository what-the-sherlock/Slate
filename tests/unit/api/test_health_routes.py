from fastapi.testclient import TestClient

from tests.fakes.system import FakeHealthProbe


def test_healthz_is_ok_without_touching_the_db(client: TestClient, probe: FakeHealthProbe) -> None:
    probe.db_ok = False  # liveness must not depend on the DB
    r = client.get("/api/v1/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_readyz_ready(client: TestClient) -> None:
    r = client.get("/api/v1/readyz")
    assert r.status_code == 200
    assert r.json() == {
        "status": "ready",
        "database": True,
        "migrations_current": True,
        "migration_revision": "0001",
        "expected_revision": "0001",
    }


def test_readyz_503_when_migrations_behind(client: TestClient, probe: FakeHealthProbe) -> None:
    probe.revision = None
    r = client.get("/api/v1/readyz")
    assert r.status_code == 503
    assert r.json()["status"] == "not_ready"
    assert r.json()["migrations_current"] is False


def test_readyz_503_when_db_down(client: TestClient, probe: FakeHealthProbe) -> None:
    probe.db_ok = False
    r = client.get("/api/v1/readyz")
    assert r.status_code == 503
    assert r.json()["database"] is False
