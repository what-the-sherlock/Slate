"""GitHubApi as a GitHub App (ADR-0023). Plain httpx; no GitHub SDK.

Auth: an RS256 JWT signed with the app's private key (10 min) is exchanged for a 1-hour
installation token, cached until 5 minutes before it expires. Nothing is persisted. The
user-ownership check exchanges the install redirect's OAuth code for a user token, asks
GitHub which installations that user can access, then revokes the token.

Rule 4: explicit timeouts; GETs are retried with exponential backoff and jitter and honour
Retry-After / x-ratelimit-reset; failures become domain errors. Tokens and file contents
are never logged.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import random
import re
import tarfile
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, cast
from urllib.parse import quote

import httpx
import structlog
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from core.domain.errors import NotFound, PermissionDenied, RateLimited, UpstreamUnavailable
from core.domain.github import (
    MAX_DOC_FILE_BYTES,
    MAX_DOC_FILES,
    MAX_TAR_MEMBERS,
    MAX_TARBALL_BYTES,
    MAX_UNPACKED_BYTES,
    Comment,
    Issue,
    Repo,
    RepoFile,
    is_doc_path,
    is_valid_repo_name,
)

log = structlog.get_logger(__name__)
API = "https://api.github.com"
TIMEOUT = httpx.Timeout(20.0, connect=5.0)
TARBALL_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
MAX_ATTEMPTS = 4
MAX_WAIT_S = 60
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')
_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


_PEM = re.compile(r"-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----", re.S)
NL = chr(10)


def normalise_pem(raw: str) -> str:
    """A valid PEM from however the key was pasted into an env var: real newlines, literal
    backslash-n, newlines turned into spaces, surrounding quotes, or the whole file
    base64-encoded (secret fields mangle multi-line values in all of these ways)."""
    text = raw.strip().strip('"').strip("'").replace(chr(92) + "n", NL).replace(chr(13), "")
    if "-----BEGIN" not in text:
        try:
            text = base64.b64decode("".join(text.split()), validate=True).decode()
        except ValueError:
            raise ValueError("not a PEM key") from None
    m = _PEM.search(text)
    if not m:
        raise ValueError("not a PEM key")
    label, body = m.group(1), "".join(m.group(2).split())
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    return NL.join([f"-----BEGIN {label}-----", *lines, f"-----END {label}-----", ""])


def app_jwt(app_id: str, private_key_pem: str, now: float | None = None) -> str:
    """A GitHub App JWT (RS256), valid ~10 minutes, backdated 60 s for clock drift."""
    issued = int(now if now is not None else time.time()) - 60
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({"iat": issued, "exp": issued + 540, "iss": app_id}).encode())
    key = serialization.load_pem_private_key(normalise_pem(private_key_pem).encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("GITHUB_APP_PRIVATE_KEY must be an RSA key")
    signature = key.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64url(signature)}"


def _time(value: Any) -> datetime:  # reason: JSON values are Any
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _field(obj: Any, key: str) -> Any:  # reason: JSON values are Any
    return cast(dict[str, Any], obj).get(key) if isinstance(obj, dict) else None


def _login(obj: Any) -> str | None:
    login = _field(_field(obj, "user"), "login")
    return login if isinstance(login, str) else None


def _labels(obj: Any) -> tuple[str, ...]:
    raw = _field(obj, "labels")
    items = cast(list[Any], raw) if isinstance(raw, list) else []
    return tuple(str(n) for lab in items if (n := _field(lab, "name")) is not None)


def _repo(r: Any) -> Repo:
    return Repo(
        id=int(r["id"]),
        full_name=str(r["full_name"]),
        private=bool(r["private"]),
        default_branch=str(r.get("default_branch") or "main"),
        html_url=str(r["html_url"]),
        description=r.get("description"),
    )


class GitHubApp:
    def __init__(
        self,
        *,
        app_id: str | None,
        slug: str | None,
        client_id: str | None,
        client_secret: str | None,
        private_key: str | None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._app_id = app_id
        self._slug = slug
        self._client_id = client_id
        self._client_secret = client_secret
        self._key = private_key.replace("\\n", "\n") if private_key else None  # env var form
        self._client = client
        self._tokens: dict[int, tuple[str, float]] = {}

    @property
    def configured(self) -> bool:
        return bool(
            self._app_id and self._slug and self._client_id and self._client_secret and self._key
        )

    def install_url(self, state: str) -> str:
        slug = quote(self._slug or "")
        return f"https://github.com/apps/{slug}/installations/new?state={quote(state)}"

    # ------------------------------------------------------------------ HTTP

    async def _send(
        self,
        method: str,
        url: str,
        *,
        retry: bool,
        http_timeout: httpx.Timeout = TIMEOUT,
        **kw: Any,
    ) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(MAX_ATTEMPTS if retry else 1):
            try:
                if self._client is not None:
                    response = await self._client.request(method, url, timeout=http_timeout, **kw)
                else:
                    # No redirects: these calls carry a token, which must only reach GitHub.
                    async with httpx.AsyncClient(timeout=http_timeout, follow_redirects=False) as c:
                        response = await c.request(method, url, **kw)
            except httpx.HTTPError as exc:
                last = exc
                await asyncio.sleep(min(2**attempt, 8) * random.uniform(0.5, 1.0))  # noqa: S311
                continue
            if response.status_code in (429, 403) and _rate_limited(response):
                wait = _retry_after(response)
                if not retry or attempt == MAX_ATTEMPTS - 1 or wait > MAX_WAIT_S:
                    raise RateLimited(
                        "GitHub's rate limit is reached; syncing resumes later.", retry_after_s=wait
                    )
                await asyncio.sleep(wait)
                continue
            if response.status_code >= 500 and retry and attempt < MAX_ATTEMPTS - 1:
                await asyncio.sleep(min(2**attempt, 8) * random.uniform(0.5, 1.0))  # noqa: S311
                continue
            return response
        log.warning("github.unavailable", error=type(last).__name__ if last else "5xx")
        raise UpstreamUnavailable("GitHub is unavailable right now.")

    async def _installation_token(self, installation_id: int) -> str:
        cached = self._tokens.get(installation_id)
        if cached and cached[1] - time.time() > 300:
            return cached[0]
        if not self._app_id or not self._key:
            raise UpstreamUnavailable("The GitHub App isn't configured on this server.")
        try:
            jwt = app_jwt(self._app_id, self._key)
        except ValueError:
            # A config problem, not the user's: say so plainly (never echo the key).
            log.error("github.private_key_invalid")
            raise UpstreamUnavailable(
                "The GitHub App's private key on the server is malformed. "
                "Paste the whole .pem file into GITHUB_APP_PRIVATE_KEY."
            ) from None
        r = await self._send(
            "POST",
            f"{API}/app/installations/{installation_id}/access_tokens",
            retry=True,
            headers={**_HEADERS, "Authorization": f"Bearer {jwt}"},
        )
        if r.status_code in (404, 403):
            raise NotFound("Slate's GitHub App is no longer installed there. Reconnect GitHub.")
        _ok(r)
        body = r.json()
        self._tokens[installation_id] = (str(body["token"]), _time(body["expires_at"]).timestamp())
        return str(body["token"])

    async def _get(self, installation_id: int, url: str, **kw: Any) -> httpx.Response:
        token = await self._installation_token(installation_id)
        r = await self._send(
            "GET", url, retry=True, headers={**_HEADERS, "Authorization": f"Bearer {token}"}, **kw
        )
        if r.status_code == 404:
            raise NotFound(
                "That repository isn't available to Slate. Check the app's repository access."
            )
        _ok(r)
        return r

    async def _pages(
        self, installation_id: int, url: str, params: dict[str, Any]
    ) -> AsyncIterator[Any]:
        next_url: str | None = url
        query: dict[str, Any] | None = params
        while next_url:
            r = await self._get(installation_id, next_url, params=query)
            yield r.json()
            next_url, query = _next_link(r), None

    # ------------------------------------------------------------------ ownership

    async def user_installation_repos(self, code: str, installation_id: int) -> list[str] | None:
        if not code or len(code) > 200:
            return None
        r = await self._send(
            "POST",
            "https://github.com/login/oauth/access_token",
            retry=False,  # an OAuth code is single use
            headers={"Accept": "application/json"},
            data={"client_id": self._client_id, "client_secret": self._client_secret, "code": code},
        )
        token = r.json().get("access_token") if r.status_code == 200 else None
        if not token:
            log.info("github.oauth_code_rejected", status=r.status_code)
            return None
        try:
            owns = False
            async for page in self._user_pages(
                str(token), f"{API}/user/installations?per_page=100"
            ):
                if any(int(i["id"]) == installation_id for i in page.get("installations", [])):
                    owns = True
                    break
            if not owns:
                return None
            # Only repos *this user* can read (an org install may grant the app more).
            names: list[str] = []
            url = f"{API}/user/installations/{installation_id}/repositories?per_page=100"
            async for page in self._user_pages(str(token), url):
                names.extend(str(r["full_name"]) for r in page.get("repositories", []))
                if len(names) >= 1000:
                    break
            return names
        finally:
            await self._revoke(str(token))

    async def _user_pages(self, token: str, first: str) -> AsyncIterator[Any]:
        url: str | None = first
        while url:
            r = await self._send(
                "GET", url, retry=True, headers={**_HEADERS, "Authorization": f"Bearer {token}"}
            )
            _ok(r)
            yield r.json()
            url = _next_link(r)

    async def _revoke(self, token: str) -> None:
        try:
            await self._send(
                "DELETE",
                f"{API}/applications/{self._client_id}/token",
                retry=False,
                headers=_HEADERS,
                auth=(self._client_id or "", self._client_secret or ""),
                json={"access_token": token},
            )
        except (UpstreamUnavailable, RateLimited):
            log.info("github.token_revoke_failed")  # it expires on its own (8 h)

    # ------------------------------------------------------------------ data

    async def installation_repos(self, installation_id: int) -> list[Repo]:
        repos: list[Repo] = []
        async for page in self._pages(
            installation_id, f"{API}/installation/repositories", {"per_page": 100}
        ):
            repos.extend(_repo(r) for r in page.get("repositories", []))
            if len(repos) >= 1000:
                break
        return sorted(repos, key=lambda r: r.full_name.lower())

    async def repo(self, installation_id: int, full_name: str) -> Repo:
        _check_name(full_name)
        return _repo((await self._get(installation_id, f"{API}/repos/{full_name}")).json())

    async def head_commit(self, installation_id: int, full_name: str, branch: str) -> str:
        _check_name(full_name)
        r = await self._get(
            installation_id,
            f"{API}/repos/{full_name}/commits/{quote(branch, safe='')}",
            headers={"Accept": "application/vnd.github.sha"},
        )
        return r.text.strip()

    async def doc_files(
        self, installation_id: int, full_name: str, ref: str
    ) -> AsyncIterator[RepoFile]:
        _check_name(full_name)
        token = await self._installation_token(installation_id)
        buf = io.BytesIO()
        url = f"{API}/repos/{full_name}/tarball/{quote(ref, safe='')}"
        headers = {**_HEADERS, "Authorization": f"Bearer {token}"}
        async with contextlib.AsyncExitStack() as stack:
            c = self._client or await stack.enter_async_context(
                httpx.AsyncClient(timeout=TARBALL_TIMEOUT, follow_redirects=True)
            )
            r = await stack.enter_async_context(
                c.stream("GET", url, headers=headers, timeout=TARBALL_TIMEOUT)
            )
            if r.status_code == 404:
                raise NotFound("That repository isn't available to Slate.")
            _ok(r)
            async for chunk in r.aiter_bytes():
                if buf.tell() + len(chunk) > MAX_TARBALL_BYTES:
                    raise PermissionDenied("This repository is too large to index (over 50 MB).")
                buf.write(chunk)
        buf.seek(0)
        count = 0
        walked = unpacked = 0
        # Streaming mode ("r|gz") keeps no member list in memory; the caps bound a gzip
        # bomb of millions of entries or gigabytes of padding (security review).
        with tarfile.open(fileobj=buf, mode="r|gz") as tar:
            for member in tar:
                walked += 1
                unpacked += max(member.size, 0)
                if walked > MAX_TAR_MEMBERS or unpacked > MAX_UNPACKED_BYTES:
                    log.info("github.tarball_capped", members=walked)
                    return
                if not member.isfile():  # no symlinks, hard links or devices
                    continue
                path = member.name.split("/", 1)[1] if "/" in member.name else ""
                if not path or not is_doc_path(path) or member.size > MAX_DOC_FILE_BYTES:
                    continue
                handle = tar.extractfile(member)
                if handle is None:
                    continue
                yield RepoFile(
                    path=path, data=handle.read(MAX_DOC_FILE_BYTES + 1)[:MAX_DOC_FILE_BYTES]
                )
                count += 1
                if count >= MAX_DOC_FILES:
                    log.info("github.doc_files_capped", files=count)
                    return

    async def issues(
        self, installation_id: int, full_name: str, since: str | None, limit: int
    ) -> AsyncIterator[Issue]:
        _check_name(full_name)
        params: dict[str, Any] = {
            "state": "all",
            "sort": "updated",
            "direction": "asc",
            "per_page": 100,
        }
        if since:
            params["since"] = since
        n = 0
        async for page in self._pages(installation_id, f"{API}/repos/{full_name}/issues", params):
            for i in page:
                pr = i.get("pull_request")
                yield Issue(
                    number=int(i["number"]),
                    title=str(i.get("title") or ""),
                    body=str(i.get("body") or ""),
                    state=str(i.get("state") or "open"),
                    is_pr=pr is not None,
                    author=_login(i),
                    html_url=str(i["html_url"]),
                    created_at=_time(i["created_at"]),
                    updated_at=_time(i["updated_at"]),
                    comments=int(i.get("comments") or 0),
                    labels=_labels(i),
                    merged=bool(pr and pr.get("merged_at")),
                )
                n += 1
                if n >= limit:
                    return

    async def comments(self, installation_id: int, full_name: str, number: int) -> list[Comment]:
        _check_name(full_name)
        out: list[Comment] = []
        async for page in self._pages(
            installation_id, f"{API}/repos/{full_name}/issues/{number}/comments", {"per_page": 100}
        ):
            out.extend(
                Comment(
                    _login(c),
                    str(c.get("body") or ""),
                    _time(c["created_at"]),
                )
                for c in page
            )
            break  # first 100 comments are plenty for Q&A (MAX_COMMENTS)
        return out


def _next_link(r: httpx.Response) -> str | None:
    """The next page, only if it's on GitHub's API host: our token goes with it."""
    m = _NEXT.search(r.headers.get("link", ""))
    if not m:
        return None
    url = m.group(1)
    return url if url.startswith(API + "/") else None


def _check_name(full_name: str) -> None:
    if not is_valid_repo_name(full_name):
        raise ValueError("invalid repository name")


def _rate_limited(r: httpx.Response) -> bool:
    return (
        r.status_code == 429
        or r.headers.get("x-ratelimit-remaining") == "0"
        or "retry-after" in r.headers
    )


def _retry_after(r: httpx.Response) -> int:
    if (value := r.headers.get("retry-after")) and value.isdigit():
        return int(value)
    if (reset := r.headers.get("x-ratelimit-reset")) and reset.isdigit():
        return max(1, int(reset) - int(time.time()))
    return 60


def _ok(r: httpx.Response) -> None:
    if r.status_code == 401:
        raise UpstreamUnavailable("GitHub rejected Slate's app credentials.")
    if r.status_code == 403:
        raise PermissionDenied(
            "GitHub refused access. Check the app's permissions on this repository."
        )
    if r.status_code >= 400:
        raise UpstreamUnavailable(f"GitHub request failed ({r.status_code}).")
