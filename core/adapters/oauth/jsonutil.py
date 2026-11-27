"""Defensive readers for provider JSON (ADR-0033). Provider replies are untrusted: every
reader returns an empty value of the right type instead of raising on a surprise shape."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def obj(raw: object) -> dict[str, Any]:  # reason: JSON values are Any
    return raw if isinstance(raw, dict) else {}  # type: ignore[return-value]


def objs(raw: object) -> list[dict[str, Any]]:  # reason: JSON values are Any
    return [x for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []  # type: ignore[misc]


def strs(raw: object) -> list[str]:
    return [str(x) for x in raw] if isinstance(raw, list) else []  # type: ignore[union-attr]


def text(raw: object) -> str | None:
    return raw if isinstance(raw, str) and raw else None


def when(raw: object) -> datetime | None:
    """An ISO 8601 timestamp (with "Z" or an offset), or None."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def safe_id(value: str, *, allowed: str = "-_", max_len: int = 128) -> str:
    """A provider id that may go into a URL path: letters, digits and ``allowed`` only."""
    ok = 0 < len(value) <= max_len and all(c.isalnum() or c in allowed for c in value)
    return value if ok else "invalid"
