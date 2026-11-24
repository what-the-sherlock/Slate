from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.deps import Container, get_container
from api.main import create_app
from core.config import Settings
from core.services.auth import AuthService
from tests.fakes.auth import FakeHasher, InMemoryAccounts, InMemorySessions, InMemoryThrottle
from tests.fakes.system import FakeClock, FakeHealthProbe


@pytest.fixture
def probe() -> FakeHealthProbe:
    return FakeHealthProbe(db_ok=True, revision="0001")


@pytest.fixture
def auth() -> AuthService:
    clock = FakeClock()
    accounts = InMemoryAccounts()
    return AuthService(
        accounts,
        InMemorySessions(accounts, clock),
        InMemoryThrottle(clock),
        FakeHasher(),
        clock,
    )


@pytest.fixture
def app(probe: FakeHealthProbe, auth: AuthService) -> FastAPI:
    application = create_app()
    container = Container(
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        health=probe,
        expected_revision="0001",
        auth=auth,
    )
    application.dependency_overrides[get_container] = lambda: container
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
