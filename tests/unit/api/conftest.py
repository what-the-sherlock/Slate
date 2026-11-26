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
from core.services.answer import AnswerService
from core.services.auth import AuthPolicy, AuthService
from core.services.catalog import CatalogService
from core.services.discord import DiscordService
from core.services.github import GitHubService
from core.services.search import SearchService
from core.services.source_actions import ConnectorEntry, SourceActions
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
from tests.fakes.chat import InMemoryChatStore
from tests.fakes.discord import FakeDiscord
from tests.fakes.github import FakeGitHub
from tests.fakes.jobs import FakeJobQueue
from tests.fakes.models import FakeChatModel, FakeEmbedder, WordTokenizer
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
def chat_model() -> FakeChatModel:
    return FakeChatModel("Leave is 25 days [1].")


@pytest.fixture
def chats() -> InMemoryChatStore:
    return InMemoryChatStore()


@pytest.fixture
def answers(
    search: SearchService, chat_model: FakeChatModel, chats: InMemoryChatStore
) -> AnswerService:
    return AnswerService(
        search,
        chat_model,
        chats,
        InMemoryLedger(),
        InMemoryRateLimiter(FakeClock()),
        WordTokenizer(),
        limits=SpendLimits.monthly_only(Decimal(5)),
    )


@pytest.fixture
def fake_github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def source_store() -> InMemorySources:
    return InMemorySources()


@pytest.fixture
def github(fake_github: FakeGitHub, source_store: InMemorySources) -> GitHubService:
    clock = FakeClock()
    return GitHubService(
        fake_github,
        source_store,
        FakeJobQueue(clock),
        InMemoryRateLimiter(clock),
        clock,
        secret=b"g" * 32,
    )


@pytest.fixture
def fake_discord() -> FakeDiscord:
    return FakeDiscord()


@pytest.fixture
def discord(fake_discord: FakeDiscord, source_store: InMemorySources) -> DiscordService:
    clock = FakeClock()
    return DiscordService(
        fake_discord,
        source_store,
        FakeJobQueue(clock),
        InMemoryRateLimiter(clock),
        clock,
        secret=b"d" * 32,
    )


@pytest.fixture
def app(
    probe: FakeHealthProbe,
    github: GitHubService,
    discord: DiscordService,
    source_store: InMemorySources,
    auth: AuthService,
    uploads: UploadService,
    catalog: InMemoryCatalog,
    search: SearchService,
    answers: AnswerService,
    chats: InMemoryChatStore,
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
        answers=answers,
        chats=chats,
        github=github,
        discord=discord,
        actions=SourceActions(
            source_store,
            {
                "github": ConnectorEntry("GitHub", github),
                "discord": ConnectorEntry("Discord", discord),
            },
        ),
    )
    application.dependency_overrides[get_container] = lambda: container
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
