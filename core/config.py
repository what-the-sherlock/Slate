"""Application settings: the only module that reads environment variables.

Settings are validated once at startup. In ``prod`` every required value must be
present, so a misconfigured deploy fails immediately instead of on the first request.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Self

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
    openai_chat_model: str | None = None
    openai_embed_model: str = "text-embedding-3-small"
    embed_dims: int = Field(default=1024, gt=0)
    cf_account_id: str | None = None
    cf_api_token: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    groq_api_key: SecretStr | None = None
    monthly_llm_budget_usd: float = Field(default=5.0, ge=0)

    supabase_url: str | None = None
    supabase_service_key: SecretStr | None = None
    supabase_bucket: str = "uploads"

    github_dispatch_token: SecretStr | None = None
    github_repo: str | None = None
    github_token: SecretStr | None = None
    discord_bot_token: SecretStr | None = None

    sentry_dsn: SecretStr | None = None

    # Identifies this worker process in jobs.locked_by (sync.yml sets it from the run id).
    worker_id: str | None = None

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
