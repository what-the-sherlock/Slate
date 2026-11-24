"""Domain error hierarchy.

Adapters translate vendor exceptions into these (``raise X(...) from e``); the API
maps each one to an RFC 9457 problem response in a single handler. ``code`` is the
stable identifier used in the problem ``type`` URI, so clients can rely on it.
"""

from __future__ import annotations

from collections.abc import Mapping


class AppError(Exception):
    """Base class for every expected, typed failure."""

    code: str = "app-error"
    title: str = "Application error"
    retryable: bool = False

    def __init__(self, detail: str, *, extra: Mapping[str, object] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra: dict[str, object] = dict(extra or {})


class NotFound(AppError):
    code = "not-found"
    title = "Not found"


class PermissionDenied(AppError):
    code = "permission-denied"
    title = "Permission denied"


class Unauthenticated(AppError):
    code = "unauthenticated"
    title = "Authentication required"


class ValidationFailed(AppError):
    code = "validation-failed"
    title = "Validation failed"


class Conflict(AppError):
    code = "conflict"
    title = "Conflict"


class RateLimited(AppError):
    code = "rate-limited"
    title = "Too many requests"
    retryable = True

    def __init__(self, detail: str, *, retry_after_s: int | None = None) -> None:
        super().__init__(detail, extra={"retry_after_s": retry_after_s})
        self.retry_after_s = retry_after_s


class UpstreamUnavailable(AppError):
    code = "upstream-unavailable"
    title = "Upstream service unavailable"
    retryable = True


class BudgetExceeded(AppError):
    code = "budget-exceeded"
    title = "Monthly AI budget exceeded"


class ConnectorError(AppError):
    code = "connector-error"
    title = "Connector error"

    def __init__(self, detail: str, *, retryable: bool, connector: str) -> None:
        super().__init__(detail, extra={"connector": connector})
        self.retryable = retryable
        self.connector = connector
