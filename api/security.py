"""Session cookies, identity resolution and CSRF protection for routes.

* Session cookie: HttpOnly (page JavaScript can't read it), SameSite=Lax.
* CSRF cookie: readable by our frontend, which echoes it in ``X-CSRF-Token`` on every
  state-changing request (double-submit). Another site can make the browser *send*
  our cookies but can't *read* them, so it can't forge the header.
* Outside dev the names carry the ``__Host-`` prefix, which browsers only accept with
  Secure, Path=/ and no Domain, so a subdomain can't plant cookies for us.
* Pre-login POSTs (setup, login, guest) have no session to tie a CSRF token to, so they
  require ``X-Requested-With: slate``: a cross-site form can't set custom headers, and a
  cross-origin fetch that sets one needs a CORS preflight we never grant.
"""

from __future__ import annotations

import ipaddress
from typing import Annotated

from fastapi import Depends, Request, Response

from api.deps import Container, get_container
from core.domain.auth import SESSION_TTL, Identity, Role, tokens_match
from core.domain.errors import PermissionDenied, Unauthenticated
from core.services.auth import IssuedSession

CSRF_HEADER = "x-csrf-token"
REQUESTED_WITH_HEADER = "x-requested-with"
REQUESTED_WITH_VALUE = "slate"


def cookie_names(container: Container) -> tuple[str, str]:
    """(session cookie, csrf cookie). Plain names in dev (no HTTPS on localhost)."""
    if container.settings.env == "dev":
        return "slate_session", "slate_csrf"
    return "__Host-slate_session", "__Host-slate_csrf"


def _secure(container: Container) -> bool:
    return container.settings.env != "dev"


def set_session_cookies(container: Container, response: Response, issued: IssuedSession) -> None:
    session_name, csrf_name = cookie_names(container)
    max_age = int(SESSION_TTL.total_seconds())
    secure = _secure(container)
    response.set_cookie(
        session_name, issued.session_token, max_age=max_age, httponly=True,
        secure=secure, samesite="lax", path="/",
    )  # fmt: skip
    response.set_cookie(
        csrf_name, issued.csrf_token, max_age=max_age, httponly=False,
        secure=secure, samesite="lax", path="/",
    )  # fmt: skip


def clear_session_cookies(container: Container, response: Response) -> None:
    for name in cookie_names(container):
        response.delete_cookie(name, path="/", secure=_secure(container), samesite="lax")


def client_ip(request: Request, container: Container) -> str | None:
    """The real client IP, or None if it can't be determined safely.

    Behind a proxy (Vercel) the socket peer is the proxy, so the platform's own header
    is used, and only the header named in TRUSTED_IP_HEADER (clients can't forge a
    header the platform overwrites). Values are validated; junk becomes None.
    """
    header = container.settings.trusted_ip_header
    raw = (
        request.headers.get(header, "")
        if header
        else (request.client.host if request.client else "")
    )
    candidate = raw.split(",")[0].strip()
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


async def require_same_site_request(request: Request) -> None:
    """Pre-login CSRF defence: see module docstring."""
    if request.headers.get(REQUESTED_WITH_HEADER) != REQUESTED_WITH_VALUE:
        raise PermissionDenied("Missing X-Requested-With header.")


async def optional_identity(
    request: Request, container: Annotated[Container, Depends(get_container)]
) -> Identity | None:
    session_name, _ = cookie_names(container)
    return await container.auth.authenticate(request.cookies.get(session_name))


async def require_identity(
    identity: Annotated[Identity | None, Depends(optional_identity)],
) -> Identity:
    if identity is None:
        raise Unauthenticated("Sign in to continue.")
    return identity


async def require_csrf(
    request: Request,
    identity: Annotated[Identity, Depends(require_identity)],
    container: Annotated[Container, Depends(get_container)],
) -> Identity:
    """Header must match both the CSRF cookie and the session's stored secret."""
    _, csrf_name = cookie_names(container)
    header = request.headers.get(CSRF_HEADER, "")
    cookie = request.cookies.get(csrf_name, "")
    if (
        not header
        or not tokens_match(header, cookie)
        or not tokens_match(header, identity.csrf_secret)
    ):
        raise PermissionDenied("Missing or invalid CSRF token. Reload the page and try again.")
    return identity


async def require_non_guest_mutation(
    identity: Annotated[Identity, Depends(require_csrf)],
) -> Identity:
    """Default for state-changing routes: guests are read-only (SECURITY.md)."""
    if identity.is_guest:
        raise PermissionDenied("Guests can't make changes. Sign in to continue.")
    return identity


def require_role(required: Role):
    async def dependency(
        identity: Annotated[Identity, Depends(require_non_guest_mutation)],
    ) -> Identity:
        if not identity.can(required):
            raise PermissionDenied("You don't have permission to do that.")
        return identity

    return dependency


CurrentIdentity = Annotated[Identity, Depends(require_identity)]
# State-changing routes use MutatingIdentity (guests refused). Only routes that a guest
# must also be able to call (logout) use SessionMutatingIdentity explicitly.
MutatingIdentity = Annotated[Identity, Depends(require_non_guest_mutation)]
SessionMutatingIdentity = Annotated[Identity, Depends(require_csrf)]
SameSiteRequest = Depends(require_same_site_request)
