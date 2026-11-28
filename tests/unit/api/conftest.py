from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.deps import Container, get_container
from api.main import create_app
from core.config import Settings
from core.domain.budget import SpendLimits
from core.domain.integrations import REGISTRY
from core.ports.integrations import ClientCredentials
from core.services.admin import AdminService
from core.services.answer import AnswerService
from core.services.auth import AuthPolicy, AuthService
from core.services.catalog import CatalogService
from core.services.chats import ChatHistory
from core.services.discord import DiscordService
from core.services.github import GitHubService
from core.services.integrations import IntegrationService
from core.services.mail import MailService, MboxActions
from core.services.search import SearchService
from core.services.source_actions import ConnectorEntry, SourceActions
from core.services.uploads import UploadService
from core.services.workspaces import WorkspaceService
from tests.fakes.admin import InMemoryAdminStore
from tests.fakes.auth import (
    FakeHasher,
    FakeHumanVerifier,
    FakeMailer,
    InMemoryAccounts,
    InMemoryAuthEvents,
    InMemoryCodes,
    InMemorySessions,
    InMemoryThrottle,
)
from tests.fakes.catalog import InMemoryCatalog
from tests.fakes.chat import InMemoryChatStore
from tests.fakes.credentials import InMemoryCredentials
from tests.fakes.discord import FakeDiscord
from tests.fakes.github import FakeGitHub
from tests.fakes.integrations import FakeOAuth, InMemoryConnections
from tests.fakes.jobs import FakeJobQueue
from tests.fakes.mailbox import FakeMailbox
from tests.fakes.models import FakeChatModel, FakeEmbedder, WordTokenizer
from tests.fakes.rate_limit import InMemoryRateLimiter
from tests.fakes.sources import InMemorySources
from tests.fakes.storage import InMemoryBlobStore, InMemoryIndex
from tests.fakes.system import FakeClock, FakeHealthProbe, FakeWorkerTrigger
from tests.fakes.usage import InMemoryGenerations, InMemoryLedger
from tests.fakes.workspaces import InMemoryWorkspaces


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
def auth_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def accounts(auth_clock: FakeClock) -> InMemoryAccounts:
    return InMemoryAccounts(clock=auth_clock)


@pytest.fixture
def sessions(accounts: InMemoryAccounts, auth_clock: FakeClock) -> InMemorySessions:
    return InMemorySessions(accounts, auth_clock)


ADMIN_EMAIL = "admin@slate.test"  # the platform admin (OWNER_EMAIL) in API tests


@pytest.fixture
def auth_events(auth_clock: FakeClock) -> InMemoryAuthEvents:
    return InMemoryAuthEvents(auth_clock)


@pytest.fixture
def admin(
    accounts: InMemoryAccounts,
    sessions: InMemorySessions,
    auth_events: InMemoryAuthEvents,
    auth_clock: FakeClock,
) -> AdminService:
    return AdminService(
        InMemoryAdminStore(accounts, sessions, auth_events), auth_events, sessions, auth_clock,
        owner_email=ADMIN_EMAIL,
    )  # fmt: skip


@pytest.fixture
def auth(
    auth_events: InMemoryAuthEvents,
    mailer: FakeMailer,
    human: FakeHumanVerifier,
    accounts: InMemoryAccounts,
    sessions: InMemorySessions,
    auth_clock: FakeClock,
) -> AuthService:
    clock = auth_clock
    return AuthService(
        accounts=accounts,
        sessions=sessions,
        throttle=InMemoryThrottle(clock),
        hasher=FakeHasher(),
        codes=InMemoryCodes(clock),
        rate=InMemoryRateLimiter(clock),
        human=human,
        mailer=mailer,
        clock=clock,
        policy=AuthPolicy(code_key=b"k" * 32, owner_email=ADMIN_EMAIL),
        events=auth_events,
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
def fake_mailbox() -> FakeMailbox:
    return FakeMailbox()


@pytest.fixture
def mail(fake_mailbox: FakeMailbox, source_store: InMemorySources) -> MailService:
    clock = FakeClock()
    return MailService(
        fake_mailbox, InMemoryCredentials(), source_store, FakeJobQueue(clock),
        InMemoryRateLimiter(clock), clock,
    )  # fmt: skip


@pytest.fixture
def workspaces(
    accounts: InMemoryAccounts,
    sessions: InMemorySessions,
    source_store: InMemorySources,
    mailer: FakeMailer,
    auth_clock: FakeClock,
) -> WorkspaceService:
    return WorkspaceService(
        InMemoryWorkspaces(accounts, source_store, auth_clock), sessions, source_store,
        InMemoryRateLimiter(auth_clock), auth_clock, mailer=mailer, app_url="https://slate.test",
    )  # fmt: skip


@pytest.fixture
def app(
    probe: FakeHealthProbe,
    github: GitHubService,
    discord: DiscordService,
    mail: MailService,
    workspaces: WorkspaceService,
    source_store: InMemorySources,
    auth: AuthService,
    uploads: UploadService,
    catalog: InMemoryCatalog,
    search: SearchService,
    answers: AnswerService,
    chats: InMemoryChatStore,
    integrations: IntegrationService,
    worker_trigger: FakeWorkerTrigger,
    admin: AdminService,
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
        chat_history=ChatHistory(chats, FakeClock()),
        github=github,
        discord=discord,
        mail=mail,
        workspaces=workspaces,
        integrations=integrations,
        trigger=worker_trigger,
        admin=admin,
        actions=SourceActions(
            source_store,
            {
                "github": ConnectorEntry("GitHub", github),
                "discord": ConnectorEntry("Discord", discord),
                "imap": ConnectorEntry("Email", mail),
                "gmail_mbox": ConnectorEntry("Email import", MboxActions(mail)),
            },
        ),
    )
    application.dependency_overrides[get_container] = lambda: container
    return application


@pytest.fixture
def worker_trigger() -> FakeWorkerTrigger:
    return FakeWorkerTrigger()


@pytest.fixture
def fake_oauth() -> FakeOAuth:
    return FakeOAuth()


@pytest.fixture
def connections() -> InMemoryConnections:
    return InMemoryConnections()


@pytest.fixture
def integrations(
    fake_oauth: FakeOAuth, connections: InMemoryConnections, source_store: InMemorySources
) -> IntegrationService:
    """Linear made connectable (as a provider becomes once its adapter exists)."""
    clock = FakeClock(datetime.now(UTC))
    registry = tuple(replace(p, available=True) if p.id == "linear" else p for p in REGISTRY)
    return IntegrationService(
        connections,
        fake_oauth,
        source_store,
        InMemoryRateLimiter(clock),
        clock,
        secret=b"i" * 32,
        clients={"linear": ClientCredentials("lin-id", "lin-secret")},
        redirect_uri="https://slate.test/integrations/callback",
        legacy_ready={"github": True, "discord": True, "email": True},
        registry=registry,
    )


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
