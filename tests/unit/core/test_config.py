import pytest
from pydantic import ValidationError

from core.config import Env, Settings


def make(**kwargs: object) -> Settings:
    # _env_file=None: ignore the developer's .env so tests are deterministic
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


def test_settings_defaults_are_dev() -> None:
    s = make()
    assert s.env is Env.DEV
    assert s.embed_dims == 1024
    assert s.monthly_llm_budget_usd == 5.0


def test_settings_normalises_neon_url_to_psycopg_dialect() -> None:
    s = make(database_url="postgresql://u:p@host-pooler/db?sslmode=require")
    assert s.require_database_url() == "postgresql+psycopg://u:p@host-pooler/db?sslmode=require"


def test_settings_prod_fails_fast_when_required_values_missing() -> None:
    with pytest.raises(ValidationError, match="DATABASE_URL, DATABASE_URL_DIRECT, SESSION_SECRET"):
        make(env="prod")


def test_settings_prod_rejects_short_session_secret() -> None:
    with pytest.raises(ValidationError, match="at least 32"):
        make(
            env="prod",
            database_url="postgresql://a",
            database_url_direct="postgresql://b",
            session_secret="short",
        )


def test_settings_secrets_are_masked_in_repr() -> None:
    s = make(openai_api_key="sk-very-secret")
    assert "sk-very-secret" not in repr(s)


def test_settings_require_database_url_raises_clearly_when_missing() -> None:
    with pytest.raises(RuntimeError, match="DATABASE_URL_DIRECT is not set"):
        make().require_database_url(direct=True)


def test_settings_rejects_unknown_log_level() -> None:
    with pytest.raises(ValidationError, match="invalid LOG_LEVEL"):
        make(log_level="LOUD")
