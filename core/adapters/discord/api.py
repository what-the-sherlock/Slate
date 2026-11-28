"""DiscordApi as one Slate bot (ADR-0027). Plain httpx; no Discord SDK.

Two credentials, both from env and never logged or stored per user:
* the **bot token** reads servers the bot was added to;
* the **OAuth client** id/secret exchange the "add bot" redirect's code for a user token,
  used once to prove the user added the bot and to read *their* roles, then revoked.

Rule 4: explicit timeouts; reads retry with exponential backoff and jitter; Discord's
rate limits are honoured both ways: a 429's ``retry_after`` (and the global limit), and
pre-emptively when a route bucket reports ``X-RateLimit-Remaining: 0``. Waits longer
than ``MAX_WAIT_S`` become RateLimited so the job queue retries later instead of holding
a worker. Every id is validated as a snowflake before it goes into a URL, requests never
follow redirects, and only discord.com is ever called.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, cast
from urllib.parse import urlencode, urlsplit

import httpx
import structlog

from core.domain.attachments import MAX_ATTACHMENTS_LISTED, AttachmentInfo, clean_name
from core.domain.discord import (
    BOT_PERMISSIONS,
    Channel,
    Guild,
    Member,
    Message,
    Overwrite,
    Role,
    Thread,
    is_snowflake,
)
from core.domain.errors import (
    NotFound,
    PermissionDenied,
    RateLimited,
    UnreadableFile,
    UpstreamUnavailable,
)
from core.ports.discord import InstallProof

log = structlog.get_logger(__name__)

API = "https://discord.com/api/v10"
AUTHORIZE = "https://discord.com/oauth2/authorize"
SCOPES = "bot identify guilds.members.read"
TIMEOUT = httpx.Timeout(20.0, connect=5.0)
CDN_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})  # ADR-0043
MAX_ATTEMPTS = 4
MAX_WAIT_S = 60.0
PAGE = 100
USER_AGENT = "DiscordBot (https://discord.com/developers/docs, 1.0) Slate"
_CODE = re.compile(r"^[A-Za-z0-9]{10,100}$")
_ROUTE_ID = re.compile(r"/[0-9]{15,21}")


def _check_id(value: str) -> str:
    if not is_snowflake(value):
        raise ValueError("invalid Discord id")
    return value


def _route(method: str, url: str) -> str:
    """A rate-limit route key. Discord buckets per route *and* per top-level resource
    (channel, guild), so channel and guild ids stay in the key; others are generalised."""
    path = url.split("?", 1)[0].removeprefix(API)
    parts = path.split("/")
    out: list[str] = []
    for i, p in enumerate(parts):
        major = i > 0 and parts[i - 1] in ("channels", "guilds")
        out.append(p if major or not _ROUTE_ID.fullmatch("/" + p) else "{id}")
    return method + " " + "/".join(out)


def _field(obj: Any, key: str) -> Any:  # reason: JSON values are Any
    return cast(dict[str, Any], obj).get(key) if isinstance(obj, dict) else None


def _str(obj: Any, key: str, default: str = "") -> str:  # reason: JSON values are Any
    v = _field(obj, key)
    return str(v) if v is not None else default


def _int(obj: Any, key: str, default: int = 0) -> int:  # reason: JSON values are Any
    v = _field(obj, key)
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _list(obj: Any, key: str | None = None) -> list[Any]:  # reason: JSON values are Any
    v = obj if key is None else _field(obj, key)
    return cast(list[Any], v) if isinstance(v, list) else []


def _name(user: Any) -> str:  # reason: JSON values are Any
    return _str(user, "global_name") or _str(user, "username") or "unknown"


def parse_role(r: Any) -> Role:  # reason: JSON values are Any
    return Role(id=_str(r, "id"), name=_str(r, "name"), permissions=_int(r, "permissions"))


def parse_guild(g: Any) -> Guild:  # reason: JSON values are Any
    return Guild(
        id=_str(g, "id"),
        name=_str(g, "name", "Discord server"),
        owner_id=_str(g, "owner_id"),
        roles=tuple(parse_role(r) for r in _list(g, "roles")),
        icon=_field(g, "icon"),
    )


def parse_channel(c: Any) -> Channel:  # reason: JSON values are Any
    return Channel(
        id=_str(c, "id"),
        name=_str(c, "name", "channel"),
        type=_int(c, "type", -1),
        position=_int(c, "position"),
        parent_id=_field(c, "parent_id"),
        overwrites=tuple(
            Overwrite(
                id=_str(o, "id"),
                type=_int(o, "type"),
                allow=_int(o, "allow"),
                deny=_int(o, "deny"),
            )
            for o in _list(c, "permission_overwrites")
        ),
    )


def parse_member(m: Any) -> Member:  # reason: JSON values are Any
    return Member(
        user_id=_str(_field(m, "user"), "id"), roles=tuple(str(r) for r in _list(m, "roles"))
    )


def parse_message(m: Any) -> Message:  # reason: JSON values are Any
    author = _field(m, "author")
    ref = _field(m, "referenced_message")
    embeds: list[str] = []
    for e in _list(m, "embeds"):
        text = " ".join(t for t in (_str(e, "title"), _str(e, "description")) if t)
        if text:
            embeds.append(text)
    return Message(
        id=_str(m, "id"),
        channel_id=_str(m, "channel_id"),
        author_id=_str(author, "id"),
        author_name=_name(author),
        content=_str(m, "content"),
        type=_int(m, "type"),
        attachments=tuple(_str(a, "filename", "file") for a in _list(m, "attachments")),
        files=tuple(
            _file(m, a)
            for a in _list(m, "attachments")[:MAX_ATTACHMENTS_LISTED]
            if is_snowflake(_str(a, "id"))
        ),
        file_urls={
            _str(a, "id"): _str(a, "url")
            for a in _list(m, "attachments")
            if is_snowflake(_str(a, "id")) and _str(a, "url")
        },
        embeds=tuple(embeds),
        mentions={_str(u, "id"): _name(u) for u in _list(m, "mentions")},
        reply_to=_name(_field(ref, "author")) if isinstance(ref, dict) else None,
        edited=_field(m, "edited_timestamp") is not None,
        bot=bool(_field(author, "bot")),
    )


def _file(m: Any, a: Any) -> AttachmentInfo:  # reason: JSON values are Any
    """Tier 0 of one Discord attachment; its key is Discord's attachment id."""
    size = _field(a, "size")
    return AttachmentInfo(
        key=_str(a, "id"),
        name=clean_name(_str(a, "filename", "file")),
        mime=(_str(a, "content_type") or "application/octet-stream")[:100],
        size=size if isinstance(size, int) and size >= 0 else 0,
        ref={
            "channel": _str(m, "channel_id"),
            "message": _str(m, "id"),
            "attachment": _str(a, "id"),
        },
    )


def parse_thread(t: Any) -> Thread:  # reason: JSON values are Any
    meta = _field(t, "thread_metadata")
    return Thread(
        id=_str(t, "id"),
        parent_id=_str(t, "parent_id"),
        name=_str(t, "name", "thread"),
        last_message_id=_field(t, "last_message_id"),
        archived=bool(_field(meta, "archived")),
        private=_int(t, "type") == 12,
        message_count=_int(t, "message_count"),
    )


def _archived_at(t: Any) -> datetime | None:  # reason: JSON values are Any
    raw = _field(_field(t, "thread_metadata"), "archive_timestamp")
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")) if raw else None
    except ValueError:
        return None


class DiscordBot:
    def __init__(
        self,
        *,
        bot_token: str | None,
        client_id: str | None,
        client_secret: str | None,
        redirect_uri: str | None,
        client: httpx.AsyncClient | None = None,
        sleep: Any = asyncio.sleep,  # reason: injectable for tests (no real waits)
    ) -> None:
        self._token = bot_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect = redirect_uri
        self._client = client
        self._sleep = sleep
        self._bot_id: str | None = None
        self._buckets: dict[str, float] = {}  # route -> monotonic time it may be used again
        self._global_until = 0.0

    @property
    def configured(self) -> bool:
        return bool(self._token and self._client_id and self._client_secret and self._redirect)

    def install_url(self, state: str) -> str:
        query = urlencode(
            {
                "client_id": self._client_id or "",
                "permissions": str(BOT_PERMISSIONS),
                "scope": SCOPES,
                "response_type": "code",
                "redirect_uri": self._redirect or "",
                "state": state,
                "integration_type": "0",  # a server install, not a user app
            }
        )
        return f"{AUTHORIZE}?{query}"

    # ------------------------------------------------------------------ HTTP

    async def _wait_for(self, route: str) -> None:
        until = max(self._buckets.get(route, 0.0), self._global_until)
        delay = until - time.monotonic()
        if delay > MAX_WAIT_S:
            raise RateLimited(
                "Discord's rate limit is reached; syncing resumes later.",
                retry_after_s=int(delay) + 1,
            )
        if delay > 0:
            await self._sleep(delay)

    def _note_bucket(self, route: str, r: httpx.Response) -> None:
        if r.headers.get("x-ratelimit-remaining") == "0":
            try:
                after = float(r.headers.get("x-ratelimit-reset-after", "1"))
            except ValueError:
                after = 1.0
            self._buckets[route] = time.monotonic() + min(max(after, 0.0), 3600.0)

    async def _send(
        self, method: str, url: str, *, headers: dict[str, str], retry: bool, **kw: Any
    ) -> httpx.Response:
        route = _route(method, url)
        attempts = MAX_ATTEMPTS if retry else 1
        last: str = "5xx"
        for attempt in range(attempts):
            await self._wait_for(route)
            try:
                if self._client is not None:
                    r = await self._client.request(method, url, headers=headers, **kw)
                else:
                    # No redirects: these calls carry a token, which must only reach Discord.
                    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as c:
                        r = await c.request(method, url, headers=headers, **kw)
            except httpx.HTTPError as exc:
                last = type(exc).__name__
                if attempt < attempts - 1:
                    await self._sleep(min(2**attempt, 8) * random.uniform(0.5, 1.0))  # noqa: S311
                continue
            self._note_bucket(route, r)
            if r.status_code == 429:
                wait = self._retry_after(r)
                is_global = r.headers.get("x-ratelimit-global") == "true" or bool(
                    _field(self._json(r), "global")
                )
                if is_global:
                    self._global_until = time.monotonic() + wait
                else:
                    self._buckets[route] = time.monotonic() + wait
                if not retry or attempt == attempts - 1 or wait > MAX_WAIT_S:
                    raise RateLimited(
                        "Discord's rate limit is reached; syncing resumes later.",
                        retry_after_s=int(wait) + 1,
                    )
                continue  # _wait_for sleeps until the bucket reopens
            if r.status_code >= 500 and attempt < attempts - 1:
                await self._sleep(min(2**attempt, 8) * random.uniform(0.5, 1.0))  # noqa: S311
                continue
            return r
        log.warning("discord.unavailable", error=last)
        raise UpstreamUnavailable("Discord is unavailable right now.")

    @staticmethod
    def _json(r: httpx.Response) -> Any:  # reason: JSON values are Any
        try:
            return r.json()
        except ValueError:
            return None

    def _retry_after(self, r: httpx.Response) -> float:
        body = self._json(r)
        for raw in (_field(body, "retry_after"), r.headers.get("retry-after")):
            try:
                if raw is not None:
                    return max(float(raw), 0.1)
            except (TypeError, ValueError):
                continue
        return 5.0

    def _bot_headers(self) -> dict[str, str]:
        if not self._token:
            raise UpstreamUnavailable("Slate's Discord bot isn't configured on this server.")
        return {"Authorization": f"Bot {self._token}", "User-Agent": USER_AGENT}

    async def _get(self, path: str, **params: Any) -> Any:  # reason: JSON values are Any
        r = await self._send(
            "GET", f"{API}{path}", headers=self._bot_headers(), retry=True, params=params or None
        )
        _ok(r)
        return self._json(r)

    async def download(self, url: str, max_bytes: int) -> bytes | None:
        """An attachment's bytes from Discord's CDN (ADR-0043): https on the CDN hosts
        only, no redirects, no bot token (the link is signed), at most ``max_bytes``
        (UnreadableFile ``too_large`` past it). None when it's gone (403/404)."""
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in CDN_HOSTS or parsed.port:
            return None
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as c:
            client = self._client or c
            try:
                async with client.stream("GET", url, headers={"User-Agent": USER_AGENT}) as r:
                    if r.status_code in (403, 404, 410):
                        return None
                    if r.status_code != 200:
                        raise UpstreamUnavailable("Discord's file server is unavailable.")
                    declared = r.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise UnreadableFile("The file is too large.", reason="too_large")
                    out = bytearray()
                    async for chunk in r.aiter_bytes():
                        out += chunk
                        if len(out) > max_bytes:
                            raise UnreadableFile("The file is too large.", reason="too_large")
                    return bytes(out)
            except httpx.HTTPError as e:
                raise UpstreamUnavailable("Discord's file server is unavailable.") from e

    # ------------------------------------------------------------------ ownership

    async def verify_install(self, code: str, guild_id: str) -> InstallProof | None:
        if not _CODE.match(code or "") or not is_snowflake(guild_id) or not self.configured:
            return None
        auth = (self._client_id or "", self._client_secret or "")
        r = await self._send(
            "POST",
            f"{API}/oauth2/token",
            headers={"User-Agent": USER_AGENT},
            retry=False,  # an OAuth code is single use
            auth=auth,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._redirect or "",
            },
        )
        body = self._json(r) if r.status_code == 200 else None
        token = _field(body, "access_token")
        if not isinstance(token, str) or not token:
            log.info("discord.oauth_code_rejected", status=r.status_code)
            return None
        try:
            # Discord puts the server the bot was just added to in the token response; it
            # can only be added by someone with Manage Server there. This is the proof.
            if _str(_field(body, "guild"), "id") != guild_id:
                log.info("discord.install_guild_mismatch")
                return None
            if "guilds.members.read" not in _str(body, "scope").split():
                return None
            m = await self._send(
                "GET",
                f"{API}/users/@me/guilds/{guild_id}/member",
                headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
                retry=True,
            )
            if m.status_code != 200:
                log.info("discord.member_lookup_failed", status=m.status_code)
                return None
            member = parse_member(self._json(m))
            if not is_snowflake(member.user_id):
                return None
            return InstallProof(guild_id=guild_id, member=member)
        finally:
            await self._revoke(token, auth)

    async def _revoke(self, token: str, auth: tuple[str, str]) -> None:
        try:
            await self._send(
                "POST",
                f"{API}/oauth2/token/revoke",
                headers={"User-Agent": USER_AGENT},
                retry=False,
                auth=auth,
                data={"token": token, "token_type_hint": "access_token"},
            )
        except (UpstreamUnavailable, RateLimited):
            log.info("discord.token_revoke_failed")  # it expires on its own (7 days)

    # ------------------------------------------------------------------ server

    async def guild(self, guild_id: str) -> Guild:
        return parse_guild(await self._get(f"/guilds/{_check_id(guild_id)}"))

    async def channels(self, guild_id: str) -> list[Channel]:
        data = await self._get(f"/guilds/{_check_id(guild_id)}/channels")
        return [parse_channel(c) for c in _list(data) if is_snowflake(_str(c, "id"))]

    async def bot_member(self, guild_id: str) -> Member:
        if self._bot_id is None:
            me = await self._get("/users/@me")
            self._bot_id = _check_id(_str(me, "id"))
        data = await self._get(f"/guilds/{_check_id(guild_id)}/members/{self._bot_id}")
        return parse_member(data)

    async def leave_guild(self, guild_id: str) -> None:
        if not is_snowflake(guild_id) or not self._token:
            return
        try:
            r = await self._send(
                "DELETE",
                f"{API}/users/@me/guilds/{guild_id}",
                headers=self._bot_headers(),
                retry=True,
            )
            if r.status_code not in (204, 404):
                log.info("discord.leave_failed", status=r.status_code)
        except (UpstreamUnavailable, RateLimited):
            log.info("discord.leave_failed", status="unavailable")

    # ------------------------------------------------------------------ messages

    async def messages_after(
        self, channel_id: str, after: int, limit: int
    ) -> AsyncIterator[Message]:
        path = f"/channels/{_check_id(channel_id)}/messages"
        cursor, n = after, 0
        while n < limit:
            page = _list(await self._get(path, after=str(cursor), limit=PAGE))
            batch = sorted(
                (parse_message(m) for m in page if is_snowflake(_str(m, "id"))),
                key=lambda m: int(m.id),
            )
            batch = [m for m in batch if int(m.id) > cursor]
            for m in batch:
                yield m
                n += 1
                if n >= limit:
                    return
            if len(page) < PAGE or not batch:
                return
            cursor = int(batch[-1].id)

    async def latest_messages(self, channel_id: str, since: int, limit: int) -> list[Message]:
        path = f"/channels/{_check_id(channel_id)}/messages"
        out: list[Message] = []
        before: str | None = None
        while len(out) < limit:
            params: dict[str, Any] = {"limit": PAGE}
            if before is not None:
                params["before"] = before
            page = _list(await self._get(path, **params))
            batch = [parse_message(m) for m in page if is_snowflake(_str(m, "id"))]
            if not batch:
                break
            batch.sort(key=lambda m: int(m.id), reverse=True)
            for m in batch:
                if int(m.id) <= since or len(out) >= limit:
                    return sorted(out, key=lambda x: int(x.id))
                out.append(m)
            if len(page) < PAGE:
                break
            before = batch[-1].id
        return sorted(out, key=lambda x: int(x.id))

    # ------------------------------------------------------------------ threads

    async def active_threads(self, guild_id: str) -> list[Thread]:
        data = await self._get(f"/guilds/{_check_id(guild_id)}/threads/active")
        return [parse_thread(t) for t in _list(data, "threads") if is_snowflake(_str(t, "id"))]

    async def archived_threads(self, channel_id: str, since: datetime) -> AsyncIterator[Thread]:
        path = f"/channels/{_check_id(channel_id)}/threads/archived/public"
        before: str | None = None
        for _ in range(100):  # 10,000 threads at most; the floor stops it far sooner
            params: dict[str, Any] = {"limit": PAGE}
            if before is not None:
                params["before"] = before
            data = await self._get(path, **params)
            threads = _list(data, "threads")
            oldest: datetime | None = None
            for t in threads:
                at = _archived_at(t)
                if at is not None and at < since:
                    return
                if is_snowflake(_str(t, "id")):
                    yield parse_thread(t)
                oldest = at if at is not None else oldest
            if not _field(data, "has_more") or oldest is None:
                return
            before = oldest.isoformat()


def _ok(r: httpx.Response) -> None:
    if r.status_code < 400:
        return
    code = _int(DiscordBot._json(r), "code")  # pyright: ignore[reportPrivateUsage]
    if r.status_code == 401:
        raise UpstreamUnavailable("Discord rejected Slate's bot token.")
    if r.status_code == 403:
        raise PermissionDenied(
            "Slate's bot can't read this anymore. Check its role and the channel's permissions."
        )
    if r.status_code == 404:
        what = {10003: "channel", 10004: "server"}.get(code, "item")
        raise NotFound(f"That Discord {what} no longer exists, or Slate's bot was removed.")
    raise UpstreamUnavailable(f"Discord request failed ({r.status_code}).")
