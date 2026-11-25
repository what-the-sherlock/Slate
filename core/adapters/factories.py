"""Build adapters from Settings. Called only by composition roots (api/deps.py,
worker/main.py), so both wire identical adapters from the same configuration.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from decimal import Decimal

import structlog

from core.adapters.email.dev import AllowAllHumanVerifier, LogEmailSender
from core.adapters.email.smtp import SmtpEmailSender
from core.adapters.github.app import GitHubApp
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
from core.adapters.postgres.documents import PostgresDocumentStore
from core.adapters.postgres.jobs import PostgresJobQueue
from core.adapters.postgres.rate_limit import PostgresRateLimiter
from core.adapters.postgres.search import PostgresSearchIndex
from core.adapters.postgres.sources import PostgresSourceStore
from core.adapters.postgres.uow import UnitOfWork
from core.adapters.postgres.usage import PostgresGenerationStore, PostgresUsageLedger
from core.adapters.s3.blob_store import S3BlobStore, S3Settings
from core.adapters.security.argon2_hasher import Argon2Hasher
from core.adapters.system import SystemClock
from core.adapters.tokenizer import TiktokenTokenizer
from core.adapters.turnstile import TurnstileVerifier
from core.adapters.workersai.embedder import WorkersAIEmbedder
from core.config import Env, Settings
from core.domain.budget import SpendLimits
from core.domain.emails import OutgoingEmail
from core.domain.errors import UpstreamUnavailable
from core.ports.auth import HumanVerifier
from core.ports.email import EmailSender
from core.ports.storage import BlobStore
from core.services.answer import AnswerService
from core.services.auth import AuthPolicy, AuthService
from core.services.github import GitHubService, GitHubSync
from core.services.ingest import IngestService
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
    )


def search_service(settings: Settings, uow: UnitOfWork) -> SearchService:
    """Hybrid search. Without an OpenAI key it runs keyword-only (no spend in dev)."""
    return SearchService(
        PostgresSearchIndex(uow),
        openai_embedder(settings),
        PostgresGenerationStore(uow),
        PostgresUsageLedger(uow),
        PostgresRateLimiter(uow),
        limits=spend_limits(settings),
    )


def answer_service(settings: Settings, uow: UnitOfWork) -> AnswerService | None:
    """Cited chat, or None without an OpenAI key (chat is the one feature that needs it)."""
    key = settings.openai_api_key.get_secret_value().strip() if settings.openai_api_key else ""
    if not key:
        return None
    limiter = PostgresRateLimiter(uow)
    return AnswerService(
        search_service(settings, uow),
        OpenAIChatModel(key, model=settings.openai_chat_model or DEFAULT_CHAT_MODEL),
        PostgresChatStore(uow),
        PostgresUsageLedger(uow),
        limiter,
        TiktokenTokenizer(),
        limits=spend_limits(settings),
        min_similarity=settings.chat_min_similarity,
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
        PostgresUsageLedger(uow),
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
    )


def github_sync(settings: Settings, uow: UnitOfWork) -> GitHubSync | None:
    """The sync_github handler, or None without the app's key or an OpenAI key."""
    api = github_api(settings)
    if not (settings.github_app_id and settings.github_app_private_key):
        return None
    ingest = _ingest(settings, uow, s3_blob_store(settings) or _NoBlobs())  # type: ignore[arg-type]
    if ingest is None:
        return None
    return GitHubSync(api, PostgresSourceStore(uow), PostgresJobQueue(uow), ingest, FormatParser())


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
