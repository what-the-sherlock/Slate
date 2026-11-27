"""Build adapters from Settings. Called only by composition roots (api/deps.py,
worker/main.py), so both wire identical adapters from the same configuration.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Mapping
from decimal import Decimal
from urllib.parse import urlsplit

import structlog

from core.adapters.discord.api import DiscordBot
from core.adapters.email.dev import AllowAllHumanVerifier, LogEmailSender
from core.adapters.email.smtp import SmtpEmailSender
from core.adapters.github.app import GitHubApp
from core.adapters.github.dispatch import GitHubWorkflowTrigger, NoWorkerTrigger
from core.adapters.google.calendar import CalendarConnector
from core.adapters.google.drive import DriveConnector
from core.adapters.google.gmail import GmailConnector
from core.adapters.linear.connector import LinearConnector
from core.adapters.mail.imap import ImapMailbox
from core.adapters.mail.parse import parse_mbox
from core.adapters.notion.connector import NotionConnector
from core.adapters.oauth.client import HttpOAuthProvider
from core.adapters.openai.chat import DEFAULT_MODEL as DEFAULT_CHAT_MODEL
from core.adapters.openai.chat import OpenAIChatModel
from core.adapters.openai.embedder import OpenAIEmbedder
from core.adapters.parsers.formats import FormatParser
from core.adapters.postgres.auth import (
    PostgresAccountStore,
    PostgresCodeStore,
    PostgresLoginThrottle,
    PostgresSessionStore,
)
from core.adapters.postgres.chat import PostgresChatStore
from core.adapters.postgres.connections import PostgresConnectionStore
from core.adapters.postgres.credentials import PostgresCredentialStore, make_fernet
from core.adapters.postgres.documents import PostgresDocumentStore
from core.adapters.postgres.jobs import PostgresJobQueue
from core.adapters.postgres.rate_limit import PostgresRateLimiter
from core.adapters.postgres.search import PostgresSearchIndex
from core.adapters.postgres.sources import PostgresSourceStore
from core.adapters.postgres.uow import UnitOfWork
from core.adapters.postgres.usage import PostgresGenerationStore, PostgresUsageLedger
from core.adapters.s3.blob_store import S3BlobStore, S3Settings
from core.adapters.security.argon2_hasher import Argon2Hasher
from core.adapters.slack.connector import SlackConnector
from core.adapters.system import SystemClock
from core.adapters.tokenizer import TiktokenTokenizer
from core.adapters.turnstile import TurnstileVerifier
from core.adapters.workersai.embedder import WorkersAIEmbedder
from core.config import Env, Settings
from core.domain.budget import SpendLimits
from core.domain.emails import OutgoingEmail
from core.domain.errors import UpstreamUnavailable
from core.domain.integrations import REGISTRY
from core.ports.auth import HumanVerifier
from core.ports.email import EmailSender
from core.ports.integrations import ClientCredentials
from core.ports.jobs import WorkerTrigger
from core.ports.storage import BlobStore
from core.services.answer import AnswerService
from core.services.auth import AuthPolicy, AuthService
from core.services.connected import ConnectedSources
from core.services.discord import DiscordService
from core.services.discord_sync import DiscordSync
from core.services.github import GitHubService, GitHubSync
from core.services.ingest import IngestService
from core.services.integrations import IntegrationService
from core.services.mail import MailService, MailSync, MboxImport
from core.services.rerank import LLMReranker
from core.services.search import SearchService

log = structlog.get_logger(__name__)


def s3_blob_store(settings: Settings) -> S3BlobStore | None:
    s = settings
    if not (s.aws_endpoint_url_s3 and s.aws_access_key_id and s.aws_secret_access_key):
        return None
    return S3BlobStore(
        S3Settings(
            endpoint_url=s.aws_endpoint_url_s3,
            region=s.aws_region or "us-east-1",
            access_key_id=s.aws_access_key_id,
            secret_access_key=s.aws_secret_access_key.get_secret_value(),
            bucket=s.uploads_bucket,
        )
    )


def openai_embedder(settings: Settings) -> OpenAIEmbedder | None:
    key = settings.openai_api_key.get_secret_value().strip() if settings.openai_api_key else ""
    if not key:  # blank counts as "not configured"
        return None
    return OpenAIEmbedder(key, model=settings.openai_embed_model, dims=settings.embed_dims)


def workersai_embedder(settings: Settings) -> WorkersAIEmbedder | None:
    """BGE-M3 on Workers AI, or None without CF_ACCOUNT_ID / CF_API_TOKEN."""
    token = settings.cf_api_token.get_secret_value().strip() if settings.cf_api_token else ""
    account = (settings.cf_account_id or "").strip()
    if not (account and token):
        return None
    return WorkersAIEmbedder(account, token)


def spend_limits(settings: Settings) -> SpendLimits:
    """The four spend caps (core/domain/budget.py), from settings."""
    return SpendLimits(
        monthly_all=Decimal(str(settings.monthly_llm_budget_usd)),
        daily_all=Decimal(str(settings.daily_llm_budget_usd)),
        monthly_workspace=Decimal(str(settings.workspace_monthly_budget_usd)),
        daily_workspace=Decimal(str(settings.workspace_daily_budget_usd)),
        new_account_daily=Decimal(str(settings.new_account_daily_budget_usd)),
    )


def search_service(settings: Settings, uow: UnitOfWork) -> SearchService:
    """Hybrid search. Without an OpenAI key it runs keyword-only (no spend in dev)."""
    return SearchService(
        PostgresSearchIndex(uow),
        openai_embedder(settings),
        PostgresGenerationStore(uow),
        PostgresUsageLedger(uow, owner_email=settings.owner_email),
        PostgresRateLimiter(uow),
        limits=spend_limits(settings),
    )


def answer_service(settings: Settings, uow: UnitOfWork) -> AnswerService | None:
    """Cited chat, or None without an OpenAI key (chat is the one feature that needs it)."""
    key = settings.openai_api_key.get_secret_value().strip() if settings.openai_api_key else ""
    if not key:
        return None
    limiter = PostgresRateLimiter(uow)
    chat = OpenAIChatModel(key, model=settings.openai_chat_model or DEFAULT_CHAT_MODEL)
    return AnswerService(
        search_service(settings, uow),
        chat,
        PostgresChatStore(uow),
        PostgresUsageLedger(uow, owner_email=settings.owner_email),
        limiter,
        TiktokenTokenizer(),
        limits=spend_limits(settings),
        min_similarity=settings.chat_min_similarity,
        reranker=LLMReranker(chat) if settings.chat_rerank else None,
    )


def ingest_service(settings: Settings, uow: UnitOfWork) -> IngestService | None:
    """The upload pipeline, or None when storage or the OpenAI key isn't configured."""
    blobs = s3_blob_store(settings)
    if blobs is None:
        return None
    return _ingest(settings, uow, blobs)


def _ingest(settings: Settings, uow: UnitOfWork, blobs: BlobStore) -> IngestService | None:
    key = settings.openai_api_key.get_secret_value().strip() if settings.openai_api_key else ""
    if not key:  # blank counts as "not configured"
        return None
    return IngestService(
        blobs,
        PostgresDocumentStore(uow),
        PostgresSourceStore(uow),
        FormatParser(),
        TiktokenTokenizer(),
        OpenAIEmbedder(
            key,
            model=settings.openai_embed_model,
            dims=settings.embed_dims,
        ),
        PostgresGenerationStore(uow),
        PostgresUsageLedger(uow, owner_email=settings.owner_email),
        limits=spend_limits(settings),
    )


class _NoBlobs:
    """Connector syncs index documents directly and never touch upload storage."""

    def __getattr__(self, name: str) -> object:
        raise UpstreamUnavailable("Upload storage isn't configured on this server.")


def github_api(settings: Settings) -> GitHubApp:
    s = settings
    return GitHubApp(
        app_id=s.github_app_id,
        slug=s.github_app_slug,
        client_id=s.github_app_client_id,
        client_secret=s.github_app_client_secret.get_secret_value()
        if s.github_app_client_secret
        else None,
        private_key=s.github_app_private_key.get_secret_value()
        if s.github_app_private_key
        else None,
    )


def _github_key(settings: Settings) -> bytes:
    """Signs the install state and repo-picker grants; its own label, like the code key."""
    if settings.session_secret is None:
        return secrets.token_bytes(32)
    root = settings.session_secret.get_secret_value().encode()
    return hmac.new(root, b"slate/github/v1", hashlib.sha256).digest()


def github_service(settings: Settings, uow: UnitOfWork) -> GitHubService:
    return GitHubService(
        github_api(settings),
        PostgresSourceStore(uow),
        PostgresJobQueue(uow),
        PostgresRateLimiter(uow),
        SystemClock(),
        secret=_github_key(settings),
        credentials=credential_store(settings, uow),
    )


def github_sync(settings: Settings, uow: UnitOfWork) -> GitHubSync | None:
    """The sync_github handler, or None without the app's key or an OpenAI key."""
    api = github_api(settings)
    if not (settings.github_app_id and settings.github_app_private_key):
        return None
    ingest = _ingest(settings, uow, s3_blob_store(settings) or _NoBlobs())  # type: ignore[arg-type]
    if ingest is None:
        return None
    creds = credential_store(settings, uow)
    return GitHubSync(
        api, PostgresSourceStore(uow), PostgresJobQueue(uow), ingest, FormatParser(),
        credentials=creds if creds.available else None,
    )  # fmt: skip


def _discord_redirect(settings: Settings) -> str | None:
    """The OAuth redirect, only if it's https (or localhost in dev): the code it carries
    must never travel in clear text."""
    uri = (settings.discord_redirect_uri or "").strip()
    if not uri:
        return None
    parsed = urlsplit(uri)
    local = parsed.hostname in {"localhost", "127.0.0.1"} and settings.env is Env.DEV
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        log.warning("discord_redirect_rejected", reason="not https")
        return None
    return uri


def discord_api(settings: Settings) -> DiscordBot:
    s = settings
    return DiscordBot(
        bot_token=s.discord_bot_token.get_secret_value().strip() if s.discord_bot_token else None,
        client_id=s.discord_client_id,
        client_secret=s.discord_client_secret.get_secret_value()
        if s.discord_client_secret
        else None,
        redirect_uri=_discord_redirect(s),
    )


def _discord_key(settings: Settings) -> bytes:
    """Signs the install state and channel-picker grants; its own label (key separation)."""
    if settings.session_secret is None:
        return secrets.token_bytes(32)
    root = settings.session_secret.get_secret_value().encode()
    return hmac.new(root, b"slate/discord/v1", hashlib.sha256).digest()


def discord_service(settings: Settings, uow: UnitOfWork) -> DiscordService:
    return DiscordService(
        discord_api(settings),
        PostgresSourceStore(uow),
        PostgresJobQueue(uow),
        PostgresRateLimiter(uow),
        SystemClock(),
        secret=_discord_key(settings),
    )


def discord_sync(settings: Settings, uow: UnitOfWork) -> DiscordSync | None:
    """The sync_discord handler, or None without the bot token or an OpenAI key."""
    if not settings.discord_bot_token:
        return None
    ingest = _ingest(settings, uow, s3_blob_store(settings) or _NoBlobs())  # type: ignore[arg-type]
    if ingest is None:
        return None
    return DiscordSync(
        discord_api(settings),
        PostgresSourceStore(uow),
        PostgresJobQueue(uow),
        ingest,
        SystemClock(),
    )


def credential_store(settings: Settings, uow: UnitOfWork) -> PostgresCredentialStore:
    key = settings.credentials_fernet_key
    return PostgresCredentialStore(uow, make_fernet(key.get_secret_value() if key else None))


def mail_service(settings: Settings, uow: UnitOfWork) -> MailService:
    return MailService(
        ImapMailbox(),
        credential_store(settings, uow),
        PostgresSourceStore(uow),
        PostgresJobQueue(uow),
        PostgresRateLimiter(uow),
        SystemClock(),
    )


def mail_sync(settings: Settings, uow: UnitOfWork) -> MailSync | None:
    """The sync_mail handler, or None without the encryption key or an OpenAI key."""
    creds = credential_store(settings, uow)
    ingest = _ingest(settings, uow, s3_blob_store(settings) or _NoBlobs())  # type: ignore[arg-type]
    if not creds.available or ingest is None:
        return None
    return MailSync(
        ImapMailbox(), creds, PostgresSourceStore(uow), PostgresJobQueue(uow), ingest, SystemClock()
    )


def mbox_import(settings: Settings, uow: UnitOfWork) -> MboxImport | None:
    """The import_mbox handler, or None without storage or an OpenAI key."""
    blobs = s3_blob_store(settings)
    ingest = _ingest(settings, uow, blobs) if blobs is not None else None
    if blobs is None or ingest is None:
        return None
    return MboxImport(blobs, PostgresSourceStore(uow), ingest, parse_mbox)


def email_sender(settings: Settings) -> EmailSender | None:
    """SMTP when configured; the log stand-in only in dev; otherwise None (sign-up closes)."""
    if settings.mail_configured and settings.smtp_host and settings.mail_from:
        return SmtpEmailSender(
            host=settings.smtp_host,
            port=settings.smtp_port,
            security=settings.smtp_security,
            username=settings.smtp_username,
            password=settings.smtp_password.get_secret_value() if settings.smtp_password else None,
            sender=settings.mail_from,
        )
    return LogEmailSender() if settings.env is Env.DEV else None


def human_verifier(settings: Settings) -> HumanVerifier | None:
    """Turnstile when configured; allow-all only in dev; otherwise None (sign-up closes)."""
    if settings.turnstile_configured and settings.turnstile_secret_key:
        return TurnstileVerifier(settings.turnstile_secret_key.get_secret_value())
    return AllowAllHumanVerifier() if settings.env is Env.DEV else None


def _code_key(settings: Settings) -> bytes:
    """HMAC key for one-time codes, derived from SESSION_SECRET with its own label so it
    never equals a key used elsewhere. Without a secret (dev), a per-process random key:
    codes then stop working on restart, which is fine locally."""
    if settings.session_secret is None:
        return secrets.token_bytes(32)
    root = settings.session_secret.get_secret_value().encode()
    return hmac.new(root, b"slate/email-codes/v1", hashlib.sha256).digest()


class _Unavailable:
    """Stands in for a missing mailer or bot check outside dev, so the service can be built;
    sign-up is closed in that case and these are never reached through it."""

    async def send(self, email: OutgoingEmail) -> None:
        raise UpstreamUnavailable("Email isn't configured on this server.")

    async def verify(self, token: str, ip: str | None) -> bool:
        raise UpstreamUnavailable("The bot check isn't configured on this server.")


def auth_service(settings: Settings, uow: UnitOfWork) -> AuthService:
    """Auth wired from settings. Outside dev, sign-up stays closed unless both mail and
    Turnstile are configured: without them it would be an open door for bots (fail closed)."""
    mailer, human = email_sender(settings), human_verifier(settings)
    ready = mailer is not None and human is not None
    if settings.signups_enabled and not ready:
        log.warning(
            "signup_closed_unconfigured", mail=mailer is not None, turnstile=human is not None
        )
    missing = _Unavailable()
    return AuthService(
        accounts=PostgresAccountStore(uow),
        sessions=PostgresSessionStore(uow),
        throttle=PostgresLoginThrottle(uow),
        hasher=Argon2Hasher(),
        codes=PostgresCodeStore(uow),
        rate=PostgresRateLimiter(uow),
        human=human or missing,
        mailer=mailer or missing,
        clock=SystemClock(),
        policy=AuthPolicy(
            code_key=_code_key(settings),
            owner_email=settings.owner_email,
            setup_token=settings.setup_token.get_secret_value() if settings.setup_token else None,
            setup_token_required=settings.env is not Env.DEV,  # any reachable env
            signups_enabled=settings.signups_enabled and ready,
        ),
    )


def _integrations_key(settings: Settings) -> bytes:
    """Signs OAuth state and derives PKCE verifiers (ADR-0032); its own label."""
    if settings.session_secret is None:
        return secrets.token_bytes(32)
    root = settings.session_secret.get_secret_value().encode()
    return hmac.new(root, b"slate/integrations/v1", hashlib.sha256).digest()


def integration_service(
    settings: Settings, uow: UnitOfWork, legacy_ready: Mapping[str, bool]
) -> IntegrationService:
    key = settings.credentials_fernet_key
    return IntegrationService(
        PostgresConnectionStore(uow, make_fernet(key.get_secret_value() if key else None)),
        HttpOAuthProvider(),
        PostgresSourceStore(uow),
        PostgresRateLimiter(uow),
        SystemClock(),
        secret=_integrations_key(settings),
        clients={
            family: ClientCredentials(cid, secret)
            for family, (cid, secret) in settings.oauth_clients.items()
        },
        redirect_uri=settings.integrations_redirect_uri,
        legacy_ready=legacy_ready,
    )


def connected_sources(
    settings: Settings, uow: UnitOfWork, integrations: IntegrationService
) -> ConnectedSources:
    """The sync engine for every OAuth-connected source (ADR-0033), registered as the
    integrations' connect hook. Syncing needs only the OpenAI key (no blob storage)."""
    key = settings.credentials_fernet_key
    engine = ConnectedSources(
        PostgresSourceStore(uow),
        PostgresJobQueue(uow),
        _ingest(settings, uow, s3_blob_store(settings) or _NoBlobs()),  # type: ignore[arg-type]
        PostgresConnectionStore(uow, make_fernet(key.get_secret_value() if key else None)),
        integrations,
        PostgresRateLimiter(uow),
        SystemClock(),
        connectors={
            "gmail": GmailConnector(),
            "notion": NotionConnector(),
            "linear": LinearConnector(),
            "google_calendar": CalendarConnector(),
            "google_drive": DriveConnector(),
            "slack": SlackConnector(),
        },
        specs={p.id: p for p in REGISTRY},
        parser=FormatParser(),  # Drive PDFs, Word files and Sheets exports (P1-1)
    )
    for provider in engine.providers:
        integrations.on_connected(provider, engine.connected)
    return engine


def worker_trigger(settings: Settings) -> WorkerTrigger:
    """Starts the sync workflow when the API hands work to the worker (ADR-0035). Without
    a token and repo, work waits for the hourly cron, as before."""
    token, repo = settings.github_dispatch_token, settings.github_repo
    if token is None or not repo:
        return NoWorkerTrigger()
    try:
        return GitHubWorkflowTrigger(token=token.get_secret_value(), repo=repo.strip())
    except ValueError:
        log.error("worker_trigger_misconfigured")  # GITHUB_REPO isn't owner/name
        return NoWorkerTrigger()
