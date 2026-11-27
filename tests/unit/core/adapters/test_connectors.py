"""The OAuth source connectors (ADR-0033) against provider-shaped replies (no network)."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from core.adapters.google.calendar import CalendarConnector, event_document
from core.adapters.google.drive import DriveConnector
from core.adapters.google.gmail import GmailConnector
from core.adapters.linear.connector import LinearConnector, linear_document
from core.adapters.notion.connector import NotionConnector, notion_document
from core.adapters.oauth.api import ProviderApi
from core.adapters.slack.connector import SlackConnector, channel_documents, readable
from core.domain.errors import (
    ConnectorError,
    RateLimited,
    ReconnectRequired,
    UpstreamUnavailable,
)
from core.domain.mail import thread_external_id

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
Route = Callable[[httpx.Request], httpx.Response]


def transport(route: Route) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return route(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), seen


def ok(body: Any) -> httpx.Response:
    return httpx.Response(200, json=body)


# ---------------------------------------------------------------- shared API helper


async def test_provider_errors_map_to_typed_failures() -> None:
    cases: list[tuple[httpx.Response, type[Exception]]] = [
        (httpx.Response(401, json={}), ReconnectRequired),
        (httpx.Response(429, headers={"retry-after": "7"}, json={}), RateLimited),
        (httpx.Response(403, json={"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}),
         RateLimited),
        (httpx.Response(403, json={"error": {"errors": [{"reason": "insufficientPermissions"}]}}),
         ConnectorError),
        (httpx.Response(503, json={}), UpstreamUnavailable),
    ]  # fmt: skip
    for response, error in cases:
        client, seen = transport(lambda _r, resp=response: resp)
        api = ProviderApi("X", frozenset({"api.test"}), client, sleep=0)
        with pytest.raises(error) as caught:
            await api.get("t", "https://api.test/x")
        if response.status_code == 503:
            assert len(seen) == 3  # 5xx retried, then given up
        if response.status_code == 429:
            assert isinstance(caught.value, RateLimited)
            assert caught.value.retry_after_s == 7  # the provider's Retry-After is honoured


async def test_provider_api_only_calls_its_own_https_hosts() -> None:
    api = ProviderApi("X", frozenset({"api.test"}), httpx.AsyncClient(), sleep=0)
    for url in ("http://api.test/x", "https://evil.test/x"):
        with pytest.raises(ValueError, match="refusing"):
            await api.get("t", url)


# ---------------------------------------------------------------- Gmail


def raw_mail(subject: str, body: str, *, frm: str = "Priya <p@k.example>") -> str:
    headers = f"From: {frm}\r\nTo: me@k.example\r\nSubject: {subject}\r\n"
    msg = f"{headers}Message-ID: <{subject}@k>\r\n\r\n{body}\r\n"
    return base64.urlsafe_b64encode(msg.encode()).decode().rstrip("=")


def gmail_route(history: Any = None) -> Route:
    def route(r: httpx.Request) -> httpx.Response:
        path = r.url.path
        if path.endswith("/profile"):
            return ok({"historyId": "100"})
        if path.endswith("/threads"):
            return ok({"threads": [{"id": "t1"}, {"id": "t2"}]})
        if path.endswith("/threads/t1"):
            return ok({"messages": [
                {"id": "m1", "labelIds": ["INBOX"], "internalDate": "1764000000000"},
                {"id": "m2", "labelIds": ["SPAM"]},
                {"id": "m3", "labelIds": ["DRAFT"]},
            ]})  # fmt: skip
        if path.endswith("/threads/t2"):
            return httpx.Response(404, json={})
        if path.endswith("/messages/m1"):
            return ok({"raw": raw_mail("Invoice", "Paid on Friday.")})
        if path.endswith("/history"):
            return history if isinstance(history, httpx.Response) else ok(history)
        return httpx.Response(404, json={})

    return route


async def test_gmail_backfill_reads_threads_and_skips_spam_drafts_and_gone_threads() -> None:
    client, seen = transport(gmail_route())
    page = await GmailConnector(client, sleep=0).pull("tok", {}, now=NOW)
    [doc] = page.documents
    assert doc.external_id == thread_external_id("gm:t1")  # same ids as the IMAP connector
    assert doc.title == "Invoice"
    assert "Paid on Friday." in doc.sections[0].text
    assert page.deleted == (thread_external_id("gm:t2"),)
    assert page.done  # one page of threads, no next page token
    assert page.checkpoint["phase"] == "incremental"
    assert page.checkpoint["history_id"] == "100"  # taken before listing
    fetched = [r.url.path.rsplit("/", 1)[-1] for r in seen if "/messages/" in r.url.path]
    assert fetched == ["m1"]  # spam and draft were never downloaded
    assert all(r.headers["authorization"] == "Bearer tok" for r in seen)


async def test_gmail_incremental_rereads_touched_threads_and_moves_the_history_id() -> None:
    history = {"history": [{"messages": [{"id": "m1", "threadId": "t1"}]}], "historyId": "150"}
    client, _ = transport(gmail_route(history))
    cp = {"phase": "incremental", "history_id": "100"}
    page = await GmailConnector(client, sleep=0).pull("tok", cp, now=NOW)
    assert [d.title for d in page.documents] == ["Invoice"]
    assert page.checkpoint["history_id"] == "150"
    assert page.done


async def test_gmail_expired_history_falls_back_to_a_rescan() -> None:
    client, _ = transport(gmail_route(httpx.Response(404, json={})))
    cp = {"phase": "incremental", "history_id": "1"}
    page = await GmailConnector(client, sleep=0).pull("tok", cp, now=NOW)
    assert page.documents == ()
    assert not page.done
    assert page.checkpoint["phase"] == "backfill"
    assert page.checkpoint["history_id"] is None


async def test_gmail_refused_token_means_reconnect() -> None:
    client, _ = transport(lambda _r: httpx.Response(401, json={}))
    with pytest.raises(ReconnectRequired):
        await GmailConnector(client, sleep=0).pull("tok", {}, now=NOW)


# ---------------------------------------------------------------- Notion


def notion_page(pid: str, edited: str, title: str = "Roadmap") -> dict[str, Any]:
    return {
        "id": pid,
        "url": f"https://notion.so/{pid}",
        "created_time": "2026-11-01T10:00:00.000Z",
        "last_edited_time": edited,
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": title}]},
            "Status": {"type": "status", "status": {"name": "Doing"}},
        },
    }


def test_notion_pages_render_properties_headings_and_lists() -> None:
    blocks = [
        {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Intro."}]}},
        {"type": "heading_2", "heading_2": {"rich_text": [{"plain_text": "Q1"}]}},
        {"type": "to_do", "to_do": {"rich_text": [{"plain_text": "Ship it"}], "checked": True}},
        {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"plain_text": "UK"}]}},
    ]
    doc = notion_document(notion_page("a" * 32, "2026-11-20T10:00:00.000Z"), blocks)
    assert doc is not None
    assert doc.title == "Roadmap"
    assert [s.heading_path for s in doc.sections] == ["Roadmap", "Roadmap > Q1"]
    assert doc.sections[0].text == "Status: Doing\nIntro."
    assert doc.sections[1].text == "[x] Ship it\n- UK"


async def test_notion_incremental_stops_at_the_high_water_mark() -> None:
    pages = [
        notion_page("a" * 32, "2026-11-26T10:00:00.000Z", "New"),
        notion_page("b" * 32, "2026-11-01T10:00:00.000Z", "Old"),
    ]

    def route(r: httpx.Request) -> httpx.Response:
        if r.url.path.endswith("/search"):
            return ok({"results": pages, "has_more": True, "next_cursor": "c"})
        return ok(
            {"results": [{"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "x"}]}}]}
        )

    client, seen = transport(route)
    cp = {"mode": "incremental", "since": "2026-11-10T00:00:00+00:00", "last_full": NOW.isoformat()}
    page = await NotionConnector(client, sleep=0).pull("tok", cp, now=NOW)
    assert [d.title for d in page.documents] == ["New"]
    assert page.done  # reached a page older than the mark: no need to read on
    assert page.keep_only is None
    assert seen[0].headers["notion-version"] == "2022-06-28"


async def test_notion_full_pass_lists_what_still_exists() -> None:
    def route(r: httpx.Request) -> httpx.Response:
        if r.url.path.endswith("/search"):
            return ok(
                {"results": [notion_page("a" * 32, "2026-11-26T10:00:00.000Z")], "has_more": False}
            )
        return ok(
            {"results": [{"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "x"}]}}]}
        )

    client, _ = transport(route)
    page = await NotionConnector(client, sleep=0).pull("tok", {}, now=NOW)
    assert page.keep_only == (f"notion:{'a' * 32}",)
    assert page.checkpoint["last_full"] == NOW.isoformat()


# ---------------------------------------------------------------- Linear


def test_linear_issue_documents_carry_facts_and_comments() -> None:
    doc = linear_document({
        "id": "i1", "identifier": "ENG-42", "title": "Fix login", "description": "Steps...",
        "url": "https://linear.app/x/ENG-42", "updatedAt": "2026-11-26T10:00:00.000Z",
        "state": {"name": "In Progress"}, "assignee": {"name": "Mei"}, "team": {"name": "Eng"},
        "labels": {"nodes": [{"name": "bug"}]}, "creator": {"name": "Ada"},
        "comments": {"nodes": [{"body": "On it", "createdAt": "2026-11-26T11:00:00Z",
                                "user": {"name": "Mei"}}]},
    })  # fmt: skip
    assert doc is not None
    assert doc.title == "ENG-42: Fix login"
    assert doc.sections[0].text.startswith(
        "Status: In Progress · Assignee: Mei · Team: Eng · Labels: bug"
    )
    assert doc.sections[1].heading_path == "ENG-42: Fix login > Comment by Mei on 2026-11-26"
    assert doc.author == "Ada"


async def test_linear_graphql_errors_are_typed() -> None:
    for code, error in (("RATELIMITED", RateLimited), ("AUTHENTICATION_ERROR", ReconnectRequired)):
        body = {"errors": [{"message": "x", "extensions": {"code": code}}]}
        client, _ = transport(lambda _r, b=body: ok(b))
        with pytest.raises(error):
            await LinearConnector(client, sleep=0).pull("tok", {}, now=NOW)


# ---------------------------------------------------------------- Google Calendar


def test_events_are_dated_by_when_they_happen_and_cancelled_ones_dropped() -> None:
    event = {
        "id": "e1", "summary": "Board review", "status": "confirmed",
        "start": {"dateTime": "2026-12-02T15:00:00+00:00"},
        "end": {"dateTime": "2026-12-02T16:00:00+00:00"},
        "organizer": {"email": "ines@k.example"},
        "attendees": [{"displayName": "Ada", "responseStatus": "accepted"}],
        "description": "<b>Agenda</b> Q4 numbers",
    }  # fmt: skip
    doc = event_document(event)
    assert doc is not None
    assert doc.updated_at == datetime(2026, 12, 2, 15, 0, tzinfo=UTC)
    text = doc.sections[0].text
    assert "When: Wednesday 02 December 2026 15:00 to 16:00" in text
    assert "Attendees: Ada (accepted)" in text
    assert "Agenda" in text
    assert "<b>" not in text
    assert event_document({**event, "status": "cancelled"}) is None


def test_all_day_events_use_googles_exclusive_end_date() -> None:
    doc = event_document({"id": "e2", "summary": "Offsite", "start": {"date": "2027-03-12"},
                          "end": {"date": "2027-03-14"}})  # fmt: skip
    assert doc is not None
    assert "Friday 12 March 2027 to Saturday 13 March 2027 (all day)" in doc.sections[0].text


async def test_calendar_passes_end_with_the_events_that_still_exist() -> None:
    def route(r: httpx.Request) -> httpx.Response:
        if "pageToken" in r.url.params:
            return ok({"items": [{"id": "e2", "summary": "B", "start": {"date": "2026-12-01"}}]})
        return ok({"items": [{"id": "e1", "summary": "A", "start": {"date": "2026-11-30"}}],
                   "nextPageToken": "p2"})  # fmt: skip

    client, seen = transport(route)
    connector = CalendarConnector(client, sleep=0)
    first = await connector.pull("tok", {}, now=NOW)
    assert not first.done
    second = await connector.pull("tok", first.checkpoint, now=NOW)
    assert second.keep_only == ("gcal:e1", "gcal:e2")
    assert seen[0].url.params["timeMin"] == seen[1].url.params["timeMin"]  # one fixed window


# ---------------------------------------------------------------- Google Drive


async def test_drive_exports_google_docs_and_skips_other_types_and_big_files() -> None:
    files = [
        {"id": "d1", "name": "Plan", "mimeType": "application/vnd.google-apps.document",
         "modifiedTime": "2026-11-26T10:00:00Z"},
        {"id": "d2", "name": "notes.txt", "mimeType": "text/plain", "size": str(50 * 1024 * 1024),
         "modifiedTime": "2026-11-25T10:00:00Z"},
    ]  # fmt: skip

    def route(r: httpx.Request) -> httpx.Response:
        if r.url.path.endswith("/d1/export"):
            return httpx.Response(200, content=b"\xef\xbb\xbfLaunch in March.")
        return ok({"files": files})

    client, seen = transport(route)
    page = await DriveConnector(client, sleep=0).pull("tok", {}, now=NOW)
    [doc] = page.documents
    assert doc.title == "Plan"
    assert doc.sections[0].text == "Launch in March."
    assert page.keep_only == ("gdrive:d1", "gdrive:d2")  # the big file still exists
    assert not any("d2" in r.url.path for r in seen)  # never downloaded
    assert "modifiedTime > '2025-11-27T12:00:00+00:00'" in seen[0].url.params["q"]


# ---------------------------------------------------------------- Slack


def test_slack_markup_becomes_readable_text() -> None:
    users = {"U1": "Mei"}
    raw = "<@U1> see <#C9|eng> and <https://x.test|the doc> <!here> &amp; done"
    assert readable(raw, users) == "@Mei see #eng and the doc (https://x.test) @here & done"


def test_slack_messages_become_one_document_per_channel_day() -> None:
    msgs = [
        {"ts": "1764151200.000100", "user": "U1", "text": "Deploy at 5", "reply_count": 2},
        {"ts": "1764154800.000200", "user": "U2", "text": "ok"},
        {"ts": "1764237600.000300", "user": "U1", "text": "Next day"},
        {"ts": "1764154900.000400", "subtype": "channel_join", "text": "joined"},
    ]
    docs = channel_documents("C1", "eng", "Kestrel", msgs, {"U1": "Mei", "U2": "Arjun"})
    assert [d.external_id for d in docs] == ["slack:C1:2025-11-26", "slack:C1:2025-11-27"]
    assert docs[0].sections[0].text.splitlines() == [
        "Mei (10:00): Deploy at 5 (2 replies)",
        "Arjun (11:00): ok",
    ]
    assert docs[0].sections[0].heading_path == "Kestrel > #eng > 2025-11-26"


async def test_slack_removes_channels_the_user_left_and_maps_auth_errors() -> None:
    def route(r: httpx.Request) -> httpx.Response:
        method = r.url.path.rsplit("/", 1)[-1]
        if method == "conversations.list":
            channels = [
                {"id": "C1", "name": "eng", "is_member": True},
                {"id": "C2", "name": "x", "is_member": False},
            ]
            return ok({"ok": True, "channels": channels})
        if method == "auth.test":
            return ok({"ok": True, "team": "Kestrel"})
        if method == "users.list":
            return ok({"ok": True, "members": []})
        return ok({"ok": True, "messages": []})

    client, _ = transport(route)
    cp = {"channels": {"C1": "1764151200.0", "C3": "1764151200.0"}}
    page = await SlackConnector(client, sleep=0).pull("tok", cp, now=NOW)
    assert page.deleted_prefixes == ("slack:C3:",)  # left #C3
    assert page.checkpoint["queue"] == []
    assert page.done

    client, _ = transport(lambda _r: ok({"ok": False, "error": "token_revoked"}))
    with pytest.raises(ReconnectRequired):
        await SlackConnector(client, sleep=0).pull("tok", {}, now=NOW)
    assert json.dumps(cp)  # checkpoints stay plain JSON
