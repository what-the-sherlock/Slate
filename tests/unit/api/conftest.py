from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.deps import Container, get_container
from api.main import create_app
from core.config import Settings
from tests.fakes.system import FakeHealthProbe


@pytest.fixture
def probe() -> FakeHealthProbe:
    return FakeHealthProbe(db_ok=True, revision="0001")


@pytest.fixture
def app(probe: FakeHealthProbe) -> FastAPI:
    application = create_app()
    application.dependency_overrides[get_container] = lambda: Container(
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        health=probe,
        expected_revision="0001",
    )
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
