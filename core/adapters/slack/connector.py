"""Slack over the Web API with a user token (ADR-0033). Read-only (``channels:history``,
``channels:read``, ``users:read``).

Indexed: public channels the user is a member of. One document per channel per day (UTC),
top-level messages in order ("Name (14:05): text", with "(3 replies)"); thread replies come
later. Mentions, channel links and URLs are made readable.

A pass walks the member channels one per pull (``queue`` in the checkpoint). Per channel it
re-reads from the start of the day before its last-seen message (edits and late replies land
in recent days), or the last 90 days on first sight, and rebuilds every day it touched.
Channels the user left or that were archived are removed (``deleted_prefixes``).

Slack reports failures as HTTP 200 with ``{"ok": false, "error": ...}``: those codes are
mapped here (a revoked token means reconnect; ``ratelimited`` honours Retry-After via 429).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from core.adapters.oauth.api import ProviderApi
from core.adapters.oauth.jsonutil import obj, objs, strs, text
from core.domain.connected import FetchedDocument, Page, cap_sections
from core.domain.documents import Section
from core.domain.errors import ConnectorError, RateLimited, ReconnectRequired

API = "https://slack.com/api"
BACKFILL = timedelta(days=90)
MAX_HISTORY_PAGES = 10
MAX_CHANNELS = 200
_RECONNECT = {"invalid_auth", "token_revoked", "token_expired", "account_inactive", "not_authed"}
_SKIP_SUBTYPES = {"channel_join", "channel_leave", "channel_topic", "channel_purpose", "bot_add"}
_USER = re.compile(r"<@([A-Z0-9]{2,20})(?:\|[^>]*)?>")
_CHANNEL = re.compile(r"<#([A-Z0-9]{2,20})(?:\|([^>]*))?>")
_LINK = re.compile(r"<(https?://[^|>]{1,500})(?:\|([^>]{0,300}))?>")
_SPECIAL = re.compile(r"<!(here|channel|everyone)(?:\|[^>]*)?>")


class SlackConnector:
    def __init__(self, client: httpx.AsyncClient | None = None, *, sleep: float = 1.0) -> None:
        self._api = ProviderApi("Slack", frozenset({"slack.com"}), client, sleep=sleep)

    async def pull(self, token: str, checkpoint: Mapping[str, object], *, now: datetime) -> Page:
        cp = dict(checkpoint)
        cursors = {str(k): str(v) for k, v in obj(cp.get("channels")).items()}
        names = {str(k): str(v) for k, v in obj(cp.get("names")).items()}
        queue = strs(cp.get("queue"))
        removed: tuple[str, ...] = ()
        if not queue:  # a new pass: who are we in, now?
            team = text(cp.get("team"))
            member = await self._channels(token)
            names = dict(member)
            queue = [cid for cid, _ in member]
            gone = [cid for cid in cursors if cid not in names]
            removed = tuple(f"slack:{cid}:" for cid in gone)
            cursors = {cid: ts for cid, ts in cursors.items() if cid in names}
            if team is None:
                team = await self._team(token)
            cp["team"] = team
            if not queue:
                return Page(checkpoint={**cp, "channels": cursors, "names": names, "queue": []},
                            done=True, deleted_prefixes=removed)  # fmt: skip
        channel, rest = queue[0], queue[1:]
        users = await self._users(token)
        oldest = _reread_from(cursors.get(channel), now)
        messages = await self._history(token, channel, oldest)
        docs = channel_documents(channel, names.get(channel, "channel"), text(cp.get("team")),
                                 messages, users)  # fmt: skip
        latest = max((str(m.get("ts")) for m in messages), default=cursors.get(channel), key=_ts)
        if latest:
            cursors[channel] = latest
        next_cp = {**cp, "channels": cursors, "names": names, "queue": rest}
        return Page(tuple(docs), checkpoint=next_cp, done=not rest, deleted_prefixes=removed)

    async def _call(self, token: str, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        body = obj(await self._api.get(token, f"{API}/{method}", params=dict(params)))
        if body.get("ok") is True:
            return body
        err = str(body.get("error") or "unknown")
        if err in _RECONNECT:
            raise ReconnectRequired("Slack no longer accepts Slate's access.", connector="Slack")
        if err == "ratelimited":
            raise RateLimited("Slack's rate limit was reached; syncing resumes later.",
                              retry_after_s=60)  # fmt: skip
        raise ConnectorError(f"Slack refused the request ({err}).", retryable=False,
                             connector="Slack")  # fmt: skip

    async def _team(self, token: str) -> str | None:
        return text((await self._call(token, "auth.test", {})).get("team"))  # the team's name

    async def _channels(self, token: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        cursor = ""
        while len(out) < MAX_CHANNELS:
            params = {"types": "public_channel", "exclude_archived": "true", "limit": 200}
            body = await self._call(token, "conversations.list", {**params, "cursor": cursor})
            for c in objs(body.get("channels")):
                if c.get("is_member") and text(c.get("id")):
                    out.append((str(c["id"]), str(c.get("name") or "channel")))
            cursor = str(obj(body.get("response_metadata")).get("next_cursor") or "")
            if not cursor:
                break
        return out[:MAX_CHANNELS]

    async def _users(self, token: str) -> dict[str, str]:
        out: dict[str, str] = {}
        cursor = ""
        for _ in range(3):
            body = await self._call(token, "users.list", {"limit": 200, "cursor": cursor})
            for u in objs(body.get("members")):
                profile = obj(u.get("profile"))
                name = (
                    text(profile.get("display_name"))
                    or text(u.get("real_name"))
                    or text(u.get("name"))
                )
                if text(u.get("id")) and name:
                    out[str(u["id"])] = name
            cursor = str(obj(body.get("response_metadata")).get("next_cursor") or "")
            if not cursor:
                break
        return out

    async def _history(self, token: str, channel: str, oldest: float) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(MAX_HISTORY_PAGES):
            params = {"channel": channel, "oldest": f"{oldest:.6f}", "limit": 200, "cursor": cursor}
            body = await self._call(token, "conversations.history", params)
            out += objs(body.get("messages"))
            cursor = str(obj(body.get("response_metadata")).get("next_cursor") or "")
            if not body.get("has_more") or not cursor:
                break
        return out


def _reread_from(last_ts: str | None, now: datetime) -> float:
    """From the start of the (UTC) day before the last message seen; 90 days on first sight."""
    floor = now - BACKFILL
    if not last_ts:
        return floor.timestamp()
    last = datetime.fromtimestamp(_ts(last_ts), tz=UTC)
    day_before = (last - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(day_before, floor).timestamp()


def channel_documents(
    channel: str,
    name: str,
    team: str | None,
    messages: Sequence[Mapping[str, Any]],
    users: Mapping[str, str],
) -> list[FetchedDocument]:
    """One document per UTC day of a channel's top-level messages (pure)."""
    by_day: dict[str, list[Mapping[str, Any]]] = {}
    for m in messages:
        if m.get("subtype") in _SKIP_SUBTYPES or not text(m.get("ts")):
            continue
        day = datetime.fromtimestamp(_ts(str(m["ts"])), tz=UTC).date().isoformat()
        by_day.setdefault(day, []).append(m)
    docs: list[FetchedDocument] = []
    for day, items in sorted(by_day.items()):
        ordered = sorted(items, key=lambda m: _ts(str(m.get("ts"))))
        lines: list[str] = []
        authors: Counter[str] = Counter()
        for m in ordered:
            who = users.get(str(m.get("user") or ""), str(m.get("username") or "someone"))
            authors[who] += 1
            at = datetime.fromtimestamp(_ts(str(m["ts"])), tz=UTC)
            words = readable(str(m.get("text") or ""), users)
            replies = int(m.get("reply_count") or 0)
            more = f" ({replies} {'reply' if replies == 1 else 'replies'})" if replies else ""
            files = [str(f.get("name") or "file") for f in objs(m.get("files"))[:5]]
            attach = "".join(f"\n  [file: {f}]" for f in files)
            if words or files:
                lines.append(f"{who} ({at:%H:%M}): {words}{more}{attach}")
        if not lines:
            continue
        first = datetime.fromtimestamp(_ts(str(ordered[0]["ts"])), tz=UTC)
        last = datetime.fromtimestamp(_ts(str(ordered[-1]["ts"])), tz=UTC)
        heading = f"{team or 'Slack'} > #{name} > {day}"
        docs.append(
            FetchedDocument(
                external_id=f"slack:{channel}:{day}",
                title=f"#{name} · {first:%b %d, %Y}",
                uri=f"https://slack.com/app_redirect?channel={channel}",
                sections=cap_sections([Section(heading, "\n".join(lines))]),
                author=", ".join(n for n, _ in authors.most_common(5)) or None,
                created_at=first,
                updated_at=last,
                metadata={"kind": "slack", "messages": len(lines)},
            )
        )
    return docs


def readable(raw: str, users: Mapping[str, str]) -> str:
    """Slack markup as plain text: <@U1> -> @name, <#C1|general> -> #general, links."""
    out = _USER.sub(lambda m: "@" + users.get(m.group(1), "someone"), raw)
    out = _CHANNEL.sub(lambda m: "#" + (m.group(2) or "channel"), out)
    out = _LINK.sub(lambda m: f"{m.group(2)} ({m.group(1)})" if m.group(2) else m.group(1), out)
    out = _SPECIAL.sub(lambda m: "@" + m.group(1), out)
    return out.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").strip()


def _ts(value: str | None) -> float:
    try:
        return float(value or 0)
    except ValueError:
        return 0.0
