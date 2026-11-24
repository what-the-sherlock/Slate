import json

import pytest
import structlog

from core.logs import REDACTED, configure_logging, redact_secrets


def test_redact_secrets_masks_sensitive_keys_at_any_depth() -> None:
    event = {
        "event": "cfg",
        "api_key": "sk-1",
        "nested": {"password": "p", "ok": "fine", "items": [{"authorization": "Bearer x"}]},
        "session_secret": "s",
    }
    out = redact_secrets(None, "info", event)
    assert out["api_key"] == REDACTED
    assert out["session_secret"] == REDACTED
    assert out["nested"]["password"] == REDACTED
    assert out["nested"]["ok"] == "fine"
    assert out["nested"]["items"][0]["authorization"] == REDACTED


def test_redact_secrets_strips_credentials_from_urls() -> None:
    out = redact_secrets(None, "info", {"event": "x", "url": "postgresql://user:pw@host/db"})
    assert out["url"] == "postgresql://***@host/db"


def test_redact_secrets_keeps_innocent_keys() -> None:
    out = redact_secrets(None, "info", {"event": "x", "document_id": "abc", "count": 3})
    assert out == {"event": "x", "document_id": "abc", "count": 3}


def test_configure_logging_emits_json_without_secrets(capsys: pytest.CaptureFixture[str]) -> None:
    structlog.reset_defaults()
    configure_logging("INFO")
    structlog.get_logger("t").info("login", user_id="u1", token="abc123")
    line = json.loads(capsys.readouterr().out.strip())
    assert line["event"] == "login"
    assert line["token"] == REDACTED
    assert line["level"] == "info"
    assert "timestamp" in line
    structlog.reset_defaults()
