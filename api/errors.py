"""Map errors to RFC 9457 ``application/problem+json`` responses, in one place."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.domain.errors import (
    AppError,
    BudgetExceeded,
    Conflict,
    ConnectorError,
    NotFound,
    PermissionDenied,
    RateLimited,
    Unauthenticated,
    UpstreamUnavailable,
    ValidationFailed,
)

PROBLEM_JSON = "application/problem+json"
TYPE_BASE = "/problems/"  # relative URI reference (RFC 9457 3.1.1): no domain we do not own
log = structlog.get_logger(__name__)

STATUS_BY_ERROR: dict[type[AppError], int] = {
    NotFound: 404,
    PermissionDenied: 403,
    Unauthenticated: 401,
    ValidationFailed: 422,
    Conflict: 409,
    RateLimited: 429,
    UpstreamUnavailable: 503,
    BudgetExceeded: 402,
    ConnectorError: 502,
}


def _status_for(err: AppError) -> int:
    for cls in type(err).__mro__:
        if cls in STATUS_BY_ERROR:
            return STATUS_BY_ERROR[cls]
    return 500


def _request_id(request: Request) -> str | None:
    state: dict[str, Any] = request.scope.get("state", {})  # reason: ASGI scope is untyped
    rid = state.get("request_id")
    return rid if isinstance(rid, str) else None


def problem(
    request: Request,
    *,
    status: int,
    code: str,
    title: str,
    detail: str | None = None,
    extra: dict[str, Any] | None = None,  # reason: problem extension members are free-form
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {  # reason: JSON body
        "type": TYPE_BASE + code,
        "title": title,
        "status": status,
        "instance": request.url.path,
        "request_id": _request_id(request),
    }
    if detail:
        body["detail"] = detail
    body.update(extra or {})
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)


async def _app_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    status = _status_for(exc)
    headers: dict[str, str] = {}
    if isinstance(exc, RateLimited) and exc.retry_after_s:
        headers["Retry-After"] = str(exc.retry_after_s)
    if status >= 500:
        # Server-side problems (storage down, config missing): the detail is our own fixed
        # text, never user content, and without it a 503 can't be diagnosed from the logs.
        log.warning("app_error", code=exc.code, status=status, detail=exc.detail)
    else:
        log.info("app_error", code=exc.code, status=status)
    extra = {k: v for k, v in exc.extra.items() if v is not None}
    return problem(
        request,
        status=status,
        code=exc.code,
        title=exc.title,
        detail=exc.detail,
        extra=extra,
        headers=headers,
    )


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = [
        {"loc": [str(p) for p in e.get("loc", ())], "msg": e.get("msg", ""), "type": e.get("type")}
        for e in exc.errors()
    ]
    return problem(
        request,
        status=422,
        code=ValidationFailed.code,
        title=ValidationFailed.title,
        detail="The request is invalid.",
        extra={"errors": errors},
    )


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = {404: "not-found", 405: "method-not-allowed"}.get(exc.status_code, "http-error")
    return problem(request, status=exc.status_code, code=code, title=str(exc.detail))


async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
    # Full details go to the logs (with the request id); the client gets nothing internal.
    log.exception("unhandled_error", error_type=type(exc).__name__)
    return problem(
        request,
        status=500,
        code="internal",
        title="Internal server error",
        detail="Something went wrong on our side. Quote the request_id if reporting it.",
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _unexpected)
