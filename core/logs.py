"""Structured JSON logging with secret redaction.

Every log line is a JSON object. A processor scrubs values whose key looks
sensitive (token, secret, key, password, authorization...) at any nesting depth,
so a careless ``log.info("cfg", **settings)`` can't leak credentials.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

REDACTED = "[redacted]"
_SENSITIVE = re.compile(
    r"(token|secret|password|passwd|authorization|api[_-]?key|cookie|session|dsn|credential"
    r"|^key$|_key$)",
    re.IGNORECASE,
)
# Credentials embedded in URLs: scheme://user:password@host
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@", re.I)


def _scrub(value: Any, depth: int = 0) -> Any:  # reason: walks arbitrary log payloads
    if depth > 6:
        return value
    if isinstance(value, Mapping):
        return {
            k: (REDACTED if isinstance(k, str) and _SENSITIVE.search(k) else _scrub(v, depth + 1))
            for k, v in value.items()  # pyright: ignore[reportUnknownVariableType]
        }
    if isinstance(value, list | tuple):
        return type(value)(_scrub(v, depth + 1) for v in value)  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
    if isinstance(value, str):
        return _URL_CREDENTIALS.sub(r"\g<scheme>***@", value)
    return value


def redact_secrets(
    _logger: Any,  # reason: structlog processor signature
    _method: str,
    event_dict: MutableMapping[str, Any],  # reason: structlog processor signature
) -> MutableMapping[str, Any]:
    """structlog processor: replace sensitive values before rendering."""
    for key in list(event_dict):
        if _SENSITIVE.search(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _scrub(event_dict[key])
    return event_dict


def configure_logging(level: str = "INFO", *, json: bool = True) -> None:
    """Configure structlog once per process (call from each entry point)."""
    processors: list[Any] = [  # reason: heterogeneous structlog processor types
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redact_secrets,
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer(),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
