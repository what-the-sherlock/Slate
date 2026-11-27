"""Google Calendar over the Calendar API with an OAuth token (ADR-0033). Read-only
(``calendar.readonly``), the primary calendar.

Indexed, not looked up live (ADR-0033): a rolling window from 30 days ago to 90 days ahead,
re-read every hour. One event is one document whose *date is the event's start*, so a
question about "next week" finds next week's events through the planner's date filter.
Every pass is a full pass over the window and ends with ``keep_only``: cancelled events, and
events that left the window, leave Slate.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from core.adapters.oauth.api import ProviderApi
from core.adapters.oauth.jsonutil import obj, objs, strs, text, when
from core.domain.connected import FetchedDocument, Page, cap_sections, clean_title
from core.domain.documents import Section

URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
PAST = timedelta(days=30)
AHEAD = timedelta(days=90)
PER_PULL = 100
MAX_ATTENDEES = 30
MAX_SEEN = 10_000
_TAG = re.compile(r"<[^>]+>")


class CalendarConnector:
    def __init__(self, client: httpx.AsyncClient | None = None, *, sleep: float = 1.0) -> None:
        self._api = ProviderApi("Google Calendar", frozenset({"www.googleapis.com"}), client,
                                sleep=sleep)  # fmt: skip

    async def pull(self, token: str, checkpoint: Mapping[str, object], *, now: datetime) -> Page:
        cp = dict(checkpoint)
        page_token = text(cp.get("page_token"))
        # The window is fixed for one pass, so its pages agree.
        start = when(cp.get("window_start")) or now - PAST
        params: dict[str, Any] = {
            "timeMin": start.astimezone(UTC).isoformat(),
            "timeMax": (start + PAST + AHEAD).astimezone(UTC).isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": PER_PULL,
            "showDeleted": "false",
        }
        if page_token:
            params["pageToken"] = page_token
        listing = obj(await self._api.get(token, URL, params=params))
        seen = [s for s in strs(cp.get("seen")) if s][:MAX_SEEN] if page_token else []
        docs: list[FetchedDocument] = []
        for event in objs(listing.get("items")):
            doc = event_document(event)
            if doc is not None:
                docs.append(doc)
                seen.append(doc.external_id)
        next_token = text(listing.get("nextPageToken"))
        if next_token:
            more = {"page_token": next_token, "window_start": start.isoformat(), "seen": seen}
            return Page(tuple(docs), checkpoint=more, done=False)
        return Page(tuple(docs), checkpoint={"last_pass": now.isoformat()}, done=True,
                    keep_only=tuple(seen))  # fmt: skip


def event_document(event: Mapping[str, Any]) -> FetchedDocument | None:
    """A calendar event as a document (pure). Cancelled events are None."""
    event_id = text(event.get("id"))
    if event_id is None or event.get("status") == "cancelled":
        return None
    title = clean_title(str(event.get("summary") or ""), "(no title)")
    begins, all_day = _moment(event.get("start"))
    ends, _ = _moment(event.get("end"))
    lines = [f"When: {_span(begins, ends, all_day)}"]
    if text(event.get("location")):
        lines.append(f"Where: {event['location']}")
    organizer = obj(event.get("organizer"))
    who = text(organizer.get("displayName")) or text(organizer.get("email"))
    if who:
        lines.append(f"Organizer: {who}")
    guests = [_guest(a) for a in objs(event.get("attendees"))[:MAX_ATTENDEES]]
    if guests:
        lines.append("Attendees: " + "; ".join(g for g in guests if g))
    if text(event.get("hangoutLink")):
        lines.append(f"Video call: {event['hangoutLink']}")
    description = _TAG.sub(" ", str(event.get("description") or "")).strip()
    body = "\n".join(lines) + (f"\n\n{description}" if description else "")
    return FetchedDocument(
        external_id=f"gcal:{event_id}",
        title=title,
        uri=text(event.get("htmlLink")) or "https://calendar.google.com",
        sections=cap_sections([Section(f"Calendar > {title}", body)]),
        author=who,
        created_at=when(event.get("created")),
        updated_at=begins,  # the event's own date: "next week" means when it happens
        metadata={
            "kind": "calendar",
            "all_day": all_day,
            "recurring": bool(event.get("recurringEventId")),
        },
    )


def _moment(raw: object) -> tuple[datetime | None, bool]:
    """(start, all_day). All-day events have a date and no time; they start at UTC midnight."""
    data = obj(raw)
    if text(data.get("dateTime")):
        return when(data.get("dateTime")), False
    day = text(data.get("date"))
    if day:
        try:
            return datetime.fromisoformat(day).replace(tzinfo=UTC), True
        except ValueError:
            return None, True
    return None, False


def _span(begins: datetime | None, ends: datetime | None, all_day: bool) -> str:
    if begins is None:
        return "unknown"
    if all_day:
        last = (ends - timedelta(days=1)) if ends else begins  # Google's end date is exclusive
        if last.date() <= begins.date():
            return f"{begins:%A %d %B %Y} (all day)"
        return f"{begins:%A %d %B %Y} to {last:%A %d %B %Y} (all day)"
    end = (
        f" to {ends:%H:%M}"
        if ends and ends.date() == begins.date()
        else (f" to {ends:%A %d %B %Y %H:%M}" if ends else "")
    )
    return f"{begins:%A %d %B %Y %H:%M}{end} ({begins.tzname() or 'UTC'} offset {begins:%z})"


def _guest(a: Mapping[str, Any]) -> str:
    name = text(a.get("displayName")) or text(a.get("email")) or ""
    status = {"accepted": "accepted", "declined": "declined", "tentative": "maybe"}.get(
        str(a.get("responseStatus")), ""
    )
    return f"{name} ({status})" if name and status else name
