from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.deps import Container, get_container
from api.main import create_app
from core.config import Settings
from core.domain.budget import SpendLimits
from core.services.auth import AuthPolicy, AuthService
from core.services.catalog import CatalogService
from core.services.search import SearchService
from core.services.uploads import UploadService
from tests.fakes.auth import (
    FakeHasher,
    FakeHumanVerifier,
    FakeMailer,
    InMemoryAccounts,
    InMemoryCodes,
    InMemorySessions,
    InMemoryThrottle,
)
from tests.fakes.catalog import InMemoryCatalog
from tests.fakes.jobs import FakeJobQueue
from tests.fakes.models import FakeEmbedder
from tests.fakes.rate_limit import InMemoryRateLimiter
from tests.fakes.sources import InMemorySources
from tests.fakes.storage import InMemoryBlobStore, InMemoryIndex
from tests.fakes.system import FakeClock, FakeHealthProbe
from tests.fakes.usage import InMemoryGenerations, InMemoryLedger


@pytest.fixture
def probe() -> FakeHealthProbe:
    return FakeHealthProbe(db_ok=True, revision="0001")


@pytest.fixture
def mailer() -> FakeMailer:
    return FakeMailer()


@pytest.fixture
def human() -> FakeHumanVerifier:
    return FakeHumanVerifier()


@pytest.fixture
def auth(mailer: FakeMailer, human: FakeHumanVerifier) -> AuthService:
    clock = FakeClock()
    accounts = InMemoryAccounts(clock=clock)
    return AuthService(
        accounts=accounts,
        sessions=InMemorySessions(accounts, clock),
        throttle=InMemoryThrottle(clock),
        hasher=FakeHasher(),
        codes=InMemoryCodes(clock),
        rate=InMemoryRateLimiter(clock),
        human=human,
        mailer=mailer,
        clock=clock,
        policy=AuthPolicy(code_key=b"k" * 32),
    )


@pytest.fixture
def blobs() -> InMemoryBlobStore:
    return InMemoryBlobStore()


@pytest.fixture
def uploads(blobs: InMemoryBlobStore) -> UploadService:
    clock = FakeClock()
    return UploadService(
        blobs,
        InMemorySources(),
        FakeJobQueue(clock),
        InMemoryRateLimiter(clock),
        clock,
        root_secret=b"t" * 32,
    )


@pytest.fixture
def catalog() -> InMemoryCatalog:
    return InMemoryCatalog()


@pytest.fixture
def index() -> InMemoryIndex:
    return InMemoryIndex()


@pytest.fixture
def search(index: InMemoryIndex) -> SearchService:
    return SearchService(
        index,
        FakeEmbedder(),
        InMemoryGenerations(),  # nothing embedded: keyword lane only
        InMemoryLedger(),
        InMemoryRateLimiter(FakeClock()),
        limits=SpendLimits.monthly_only(Decimal(5)),
    )


@pytest.fixture
def app(
    probe: FakeHealthProbe,
    auth: AuthService,
    uploads: UploadService,
    catalog: InMemoryCatalog,
    search: SearchService,
) -> FastAPI:
    application = create_app()
    container = Container(
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        health=probe,
        expected_revision="0001",
        auth=auth,
        catalog=CatalogService(catalog),
        uploads=uploads,
        search=search,
    )
    application.dependency_overrides[get_container] = lambda: container
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
