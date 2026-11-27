"""Notion pages over the Notion API with an OAuth token (ADR-0033). Read-only.

Notion shows Slate only the pages the user picked on Notion's consent screen (and their
children). Each page is one document: its properties (for database rows) and its blocks as
text, a new section at every heading.

Passes:
* **Incremental** (hourly): ``/search`` newest-edited first, stopping at the last pass's
  high-water mark (``since``).
* **Full** (first sync, then weekly): every shared page, ending with ``keep_only`` so pages
  that were deleted or un-shared in Notion are removed from Slate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

import httpx

from core.adapters.oauth.api import ProviderApi
from core.adapters.oauth.jsonutil import iso, obj, objs, strs, text, when
from core.domain.connected import FetchedDocument, Page, cap_sections, clean_title
from core.domain.documents import Section

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"
PAGES_PER_PULL = 10
MAX_BLOCKS = 400
MAX_DEPTH = 2
FULL_EVERY = timedelta(days=7)
MAX_SEEN = 20_000
_TEXT_BLOCKS = (
    "paragraph", "bulleted_list_item", "numbered_list_item", "to_do", "toggle", "quote",
    "callout", "code",
)  # fmt: skip
_HEADINGS = ("heading_1", "heading_2", "heading_3")
_PREFIX = {"bulleted_list_item": "- ", "numbered_list_item": "- ", "quote": "> "}
_HEADERS = {"Notion-Version": VERSION}


class NotionConnector:
    def __init__(self, client: httpx.AsyncClient | None = None, *, sleep: float = 1.0) -> None:
        self._api = ProviderApi("Notion", frozenset({"api.notion.com"}), client, sleep=sleep)

    async def pull(self, token: str, checkpoint: Mapping[str, object], *, now: datetime) -> Page:
        cp = dict(checkpoint)
        last_full = when(cp.get("last_full"))
        full = cp.get("mode") == "full" or last_full is None or now - last_full >= FULL_EVERY
        since = None if full else when(cp.get("since"))
        body: dict[str, Any] = {
            "filter": {"property": "object", "value": "page"},
            "sort": {"direction": "descending", "timestamp": "last_edited_time"},
            "page_size": PAGES_PER_PULL,
        }
        if isinstance(cp.get("cursor"), str):
            body["start_cursor"] = cp["cursor"]
        result = obj(await self._api.post(token, f"{API}/search", json=body, headers=_HEADERS))
        seen = [s for s in strs(cp.get("seen")) if s][:MAX_SEEN] if full else []
        newest = when(cp.get("newest"))
        docs: list[FetchedDocument] = []
        reached_old = False
        for page in objs(result.get("results")):
            edited = when(page.get("last_edited_time"))
            if since is not None and edited is not None and edited <= since:
                reached_old = True
                break
            if page.get("archived") or page.get("in_trash"):
                continue
            if edited is not None and (newest is None or edited > newest):
                newest = edited
            blocks = await self._blocks(token, str(page.get("id")))
            doc = notion_document(page, blocks)
            if doc is not None:
                docs.append(doc)
                seen.append(doc.external_id)
        more = bool(result.get("has_more")) and not reached_old
        if more:
            next_cp = {
                **cp,
                "mode": "full" if full else "incremental",
                "cursor": result.get("next_cursor"),
                "newest": iso(newest),
                "seen": seen,
            }
            return Page(tuple(docs), checkpoint=next_cp, done=False)
        done_cp: dict[str, object] = {
            "mode": "incremental",
            "since": iso(newest) or cp.get("since"),
            "last_full": now.isoformat() if full else cp.get("last_full"),
        }
        keep = tuple(seen) if full else None
        return Page(tuple(docs), checkpoint=done_cp, done=True, keep_only=keep)

    async def _blocks(self, token: str, block_id: str, depth: int = 0) -> list[dict[str, Any]]:
        """Blocks in order, children inlined after their parent, bounded by MAX_BLOCKS."""
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while len(out) < MAX_BLOCKS:
            params: dict[str, Any] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            listing = await self._api.get(
                token, f"{API}/blocks/{_safe(block_id)}/children", params=params, allow_404=True,
                headers=_HEADERS,
            )  # fmt: skip
            if listing is None:
                break
            for block in objs(obj(listing).get("results")):
                out.append(block)
                kind = str(block.get("type"))
                if block.get("has_children") and depth < MAX_DEPTH and kind not in (
                    "child_page", "child_database",
                ):  # fmt: skip
                    out += await self._blocks(token, str(block.get("id")), depth + 1)
            cursor = text(obj(listing).get("next_cursor"))
            if not obj(listing).get("has_more") or not cursor:
                break
        return out[:MAX_BLOCKS]


def notion_document(
    page: Mapping[str, Any], blocks: Sequence[Mapping[str, Any]]
) -> FetchedDocument | None:
    """A Notion page as a document (pure). None when it has no text at all."""
    title = clean_title(page_title(page), "Untitled")
    sections: list[Section] = []
    props = property_lines(page)
    current_heading = title
    lines: list[str] = list(props)
    for block in blocks:
        kind = str(block.get("type"))
        words = rich_text(obj(block.get(kind)).get("rich_text"))
        if kind in _HEADINGS:
            if lines:
                sections.append(Section(current_heading, "\n".join(lines)))
            current_heading = f"{title} > {words}" if words else title
            lines = []
        elif kind in _TEXT_BLOCKS and words:
            prefix = _PREFIX.get(kind, "")
            if kind == "to_do":
                prefix = "[x] " if obj(block.get(kind)).get("checked") else "[ ] "
            lines.append(prefix + words)
    if lines:
        sections.append(Section(current_heading, "\n".join(lines)))
    capped = cap_sections(sections)
    if not capped:
        return None
    return FetchedDocument(
        external_id=f"notion:{page.get('id')}",
        title=title,
        uri=str(page.get("url") or "https://www.notion.so"),
        sections=capped,
        mime="text/markdown",
        created_at=when(page.get("created_time")),
        updated_at=when(page.get("last_edited_time")),
        metadata={"kind": "notion"},
    )


def page_title(page: Mapping[str, Any]) -> str:
    for raw in obj(page.get("properties")).values():
        prop = obj(raw)
        if prop.get("type") == "title":
            return rich_text(prop.get("title"))
    return ""


def property_lines(page: Mapping[str, Any]) -> list[str]:
    """Database-row properties as "Name: value" lines (simple types only)."""
    out: list[str] = []
    for name, raw in obj(page.get("properties")).items():
        value = _property_value(obj(raw))
        if value:
            out.append(f"{name}: {value}"[:500])
    return out[:30]


def _property_value(prop: Mapping[str, Any]) -> str:
    kind = str(prop.get("type"))
    v: object = prop.get(kind)
    if kind in ("title",):
        return ""  # it's the document title
    if kind == "rich_text":
        return rich_text(v)
    if kind in ("select", "status"):
        return str(obj(v).get("name") or "")
    if kind == "multi_select":
        return ", ".join(str(o.get("name")) for o in objs(v))
    if kind == "date":
        d = obj(v)
        return " to ".join(str(x) for x in (d.get("start"), d.get("end")) if x)
    if kind == "people":
        return ", ".join(str(p.get("name") or "") for p in objs(v) if p.get("name"))
    if kind in ("number", "url", "email", "phone_number") and v is not None:
        return str(v)
    if kind == "checkbox":
        return "yes" if v else "no"
    return ""


def rich_text(raw: object) -> str:
    return "".join(str(t.get("plain_text") or "") for t in objs(raw)).strip()


def _safe(value: str) -> str:
    """Notion ids are UUIDs (with or without dashes)."""
    v = value.replace("-", "")
    return value if v.isalnum() and len(v) == 32 else "invalid"
