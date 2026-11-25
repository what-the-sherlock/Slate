"""Application settings: the only module that reads environment variables.

Settings are validated once at startup. In ``prod`` every required value must be
present, so a misconfigured deploy fails immediately instead of on the first request.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Env(StrEnum):
    DEV = "dev"
    TEST = "test"
    PROD = "prod"


def _to_psycopg_async(url: str) -> str:
    """Normalise a Postgres URL to SQLAlchemy's psycopg 3 dialect.

    Neon hands out ``postgresql://`` URLs; SQLAlchemy needs ``postgresql+psycopg://``.
    ``channel_binding`` is a libpq option psycopg accepts, so the query string is kept.
    """
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", frozen=True
    )

    env: Env = Env.DEV
    log_level: str = "INFO"

    database_url: SecretStr | None = None
    database_url_direct: SecretStr | None = None

    session_secret: SecretStr | None = None
    credentials_fernet_key: SecretStr | None = None

    openai_api_key: SecretStr | None = None
    openai_chat_model: str | None = None  # default gpt-4o-mini (ADR-0022)
    # Answer gate (ADR-0022): below this best vector similarity, abstain without a model
    # call. Tuned on the eval set's no-answer cases.
    chat_min_similarity: float = Field(default=0.20, ge=-1, le=1)
    openai_embed_model: str = "text-embedding-3-small"
    embed_dims: int = Field(default=1024, gt=0)
    cf_account_id: str | None = None
    cf_api_token: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    groq_api_key: SecretStr | None = None
    monthly_llm_budget_usd: float = Field(default=5.0, ge=0)  # the hard stop for all users
    # Open sign-up (ADR-0021): a burst can't use the month in a day, and one workspace
    # can't crowd out the rest. See core/domain/budget.py.
    daily_llm_budget_usd: float = Field(default=0.50, ge=0)
    workspace_monthly_budget_usd: float = Field(default=0.25, ge=0)
    workspace_daily_budget_usd: float = Field(default=0.05, ge=0)

    # Neon Object Storage (ADR-0014): AWS-standard names, injected by `neon deploy`.
    aws_access_key_id: str | None = None
    aws_secret_access_key: SecretStr | None = None
    aws_endpoint_url_s3: str | None = None
    aws_region: str | None = None
    uploads_bucket: str = "uploads"

    github_dispatch_token: SecretStr | None = None
    github_repo: str | None = None
    github_token: SecretStr | None = None
    # GitHub App (ADR-0023): users install it on their repos. All five are needed for
    # the connect flow; the worker needs only the id and the private key.
    github_app_id: str | None = None
    github_app_slug: str | None = None  # from the app's public URL: github.com/apps/<slug>
    github_app_client_id: str | None = None
    github_app_client_secret: SecretStr | None = None
    github_app_private_key: SecretStr | None = None  # the .pem contents (\n escapes ok)
    discord_bot_token: SecretStr | None = None

    sentry_dsn: SecretStr | None = None

    # Identifies this worker process in jobs.locked_by (sync.yml sets it from the run id).
    worker_id: str | None = None

    # Open sign-up (ADR-0021). The verified OWNER_EMAIL account is the platform admin;
    # signing up with it needs SETUP_TOKEN, and in prod is refused while that's unset.
    owner_email: str | None = None
    setup_token: SecretStr | None = None
    signups_enabled: bool = True  # kill switch: false closes sign-up, login keeps working

    # Outgoing mail for verification and reset codes: any SMTP relay (Brevo's free tier, or
    # a Gmail app password). Unset in dev: codes are written to the API log instead.
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, gt=0, lt=65536)
    smtp_security: Literal["starttls", "ssl"] = "starttls"  # 587 = STARTTLS, 465 = SSL
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    mail_from: str | None = None  # e.g. "Slate <no-reply@example.com>"

    # Cloudflare Turnstile bot check on sign-up, resend and reset. Unset in dev: allowed.
    turnstile_site_key: str | None = None  # public; served to the browser by /auth/config
    turnstile_secret_key: SecretStr | None = None
    # Header the hosting platform sets to the real client IP, e.g. "x-vercel-forwarded-for".
    # Unset: use the socket peer. Never trust a header the client itself can set.
    trusted_ip_header: str | None = None

    @field_validator("log_level")
    @classmethod
    def _upper_level(cls, v: str) -> str:
        level = v.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid LOG_LEVEL {v!r}")
        return level

    @field_validator("database_url", "database_url_direct")
    @classmethod
    def _normalise_db_url(cls, v: SecretStr | None) -> SecretStr | None:
        return None if v is None else SecretStr(_to_psycopg_async(v.get_secret_value()))

    @model_validator(mode="after")
    def _require_in_prod(self) -> Self:
        if self.env is not Env.PROD:
            return self
        required = {
            "DATABASE_URL": self.database_url,
            "DATABASE_URL_DIRECT": self.database_url_direct,
            "SESSION_SECRET": self.session_secret,
        }
        missing = sorted(name for name, value in required.items() if value is None)
        if missing:
            raise ValueError(f"missing required settings in prod: {', '.join(missing)}")
        if self.session_secret and len(self.session_secret.get_secret_value()) < 32:
            raise ValueError("SESSION_SECRET must be at least 32 characters in prod")
        return self

    @property
    def mail_configured(self) -> bool:
        return bool(self.smtp_host and self.mail_from)

    @property
    def turnstile_configured(self) -> bool:
        return bool(self.turnstile_site_key and self.turnstile_secret_key)

    def require_api_settings(self) -> None:
        """Checks only the API needs (the worker runs with ENV=prod but serves no requests).

        Behind Vercel the socket peer is the proxy: without TRUSTED_IP_HEADER every visitor
        would share one rate-limit bucket (security review, ADR-0021).
        """
        if self.env is Env.PROD and not self.trusted_ip_header:
            raise RuntimeError("missing required setting in prod for the API: TRUSTED_IP_HEADER")

    def require_database_url(self, *, direct: bool = False) -> str:
        """Return the pooled (API) or direct (migrations, worker) URL, or fail clearly."""
        value = self.database_url_direct if direct else self.database_url
        if value is None:
            name = "DATABASE_URL_DIRECT" if direct else "DATABASE_URL"
            raise RuntimeError(f"{name} is not set")
        return value.get_secret_value()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, loaded lazily (no I/O at import time)."""
    return Settings()
