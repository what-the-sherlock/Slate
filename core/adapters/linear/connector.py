"""Linear issues over Linear's GraphQL API with an OAuth token (ADR-0033). Read-only (the
``read`` scope).

One issue is one document: identifier and title, a line of facts (status, priority,
assignee, team, project, labels), the description, then its comments.

Passes:
* **Incremental** (hourly): issues updated after the last pass's high-water mark.
* **Full** (first sync, then weekly): issues updated in the last 180 days, ending with
  ``keep_only`` so deleted or archived issues leave Slate.

Linear answers GraphQL errors with HTTP 200 and an ``errors`` list; their codes are mapped
like HTTP failures (rate limit, refused token).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import httpx

from core.adapters.oauth.api import ProviderApi
from core.adapters.oauth.jsonutil import iso, obj, objs, strs, text, when
from core.domain.connected import FetchedDocument, Page, cap_sections, clean_title
from core.domain.documents import Section
from core.domain.errors import ConnectorError, RateLimited, ReconnectRequired

URL = "https://api.linear.app/graphql"
PER_PULL = 25
FULL_EVERY = timedelta(days=7)
FULL_WINDOW = timedelta(days=180)
MAX_SEEN = 20_000

QUERY = """
query Issues($first: Int!, $after: String, $filter: IssueFilter) {
  issues(first: $first, after: $after, filter: $filter, orderBy: updatedAt) {
    nodes {
      id identifier title description url createdAt updatedAt priorityLabel
      state { name } assignee { name } creator { name } team { name } project { name }
      labels { nodes { name } }
      comments(first: 30) { nodes { body createdAt user { name } } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""


class LinearConnector:
    def __init__(self, client: httpx.AsyncClient | None = None, *, sleep: float = 1.0) -> None:
        self._api = ProviderApi("Linear", frozenset({"api.linear.app"}), client, sleep=sleep)

    async def pull(self, token: str, checkpoint: Mapping[str, object], *, now: datetime) -> Page:
        cp = dict(checkpoint)
        last_full = when(cp.get("last_full"))
        full = cp.get("mode") == "full" or last_full is None or now - last_full >= FULL_EVERY
        since = (now - FULL_WINDOW) if full else (when(cp.get("since")) or now - FULL_WINDOW)
        variables: dict[str, Any] = {
            "first": PER_PULL,
            "after": text(cp.get("cursor")),
            "filter": {"updatedAt": {"gt": since.isoformat()}},
        }
        data = await self._query(token, variables)
        issues = obj(obj(data).get("issues"))
        seen = [s for s in strs(cp.get("seen")) if s][:MAX_SEEN] if full else []
        newest = when(cp.get("newest"))
        docs: list[FetchedDocument] = []
        for node in objs(issues.get("nodes")):
            doc = linear_document(node)
            if doc is None:
                continue
            docs.append(doc)
            seen.append(doc.external_id)
            if doc.updated_at and (newest is None or doc.updated_at > newest):
                newest = doc.updated_at
        info = obj(issues.get("pageInfo"))
        if info.get("hasNextPage") and text(info.get("endCursor")):
            next_cp = {
                **cp,
                "mode": "full" if full else "incremental",
                "cursor": info["endCursor"],
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

    async def _query(self, token: str, variables: Mapping[str, Any]) -> Any:  # reason: JSON
        body = obj(await self._api.post(token, URL, json={"query": QUERY, "variables": variables}))
        errors = objs(body.get("errors"))
        if errors:
            code = str(obj(errors[0].get("extensions")).get("code") or "").upper()
            if code == "RATELIMITED":
                raise RateLimited("Linear's rate limit was reached; syncing resumes later.",
                                  retry_after_s=60)  # fmt: skip
            if code in ("AUTHENTICATION_ERROR", "FORBIDDEN"):
                raise ReconnectRequired(
                    "Linear no longer accepts Slate's access.", connector="Linear"
                )
            raise ConnectorError(f"Linear refused the request ({code or 'error'}).",
                                 retryable=False, connector="Linear")  # fmt: skip
        return body.get("data")


def linear_document(node: Mapping[str, Any]) -> FetchedDocument | None:
    """A Linear issue as a document (pure)."""
    issue_id, ident = text(node.get("id")), text(node.get("identifier"))
    if issue_id is None or ident is None:
        return None
    title = clean_title(f"{ident}: {node.get('title') or ''}", ident)
    labels = [str(n.get("name")) for n in objs(obj(node.get("labels")).get("nodes"))]
    facts = [
        f"Status: {_name(node.get('state'))}" if _name(node.get("state")) else "",
        f"Priority: {node['priorityLabel']}" if text(node.get("priorityLabel")) else "",
        f"Assignee: {_name(node.get('assignee'))}" if _name(node.get("assignee")) else "",
        f"Team: {_name(node.get('team'))}" if _name(node.get("team")) else "",
        f"Project: {_name(node.get('project'))}" if _name(node.get("project")) else "",
        f"Labels: {', '.join(labels)}" if labels else "",
    ]
    head = " · ".join(f for f in facts if f)
    body = str(node.get("description") or "").strip() or "(no description)"
    sections = [Section(title, f"{head}\n\n{body}" if head else body)]
    for c in objs(obj(node.get("comments")).get("nodes")):
        words = str(c.get("body") or "").strip()
        if words:
            who = _name(c.get("user")) or "someone"
            day = (when(c.get("createdAt")) or datetime.min).date().isoformat()
            sections.append(Section(f"{title} > Comment by {who} on {day}", words))
    return FetchedDocument(
        external_id=f"linear:{issue_id}",
        title=title,
        uri=text(node.get("url")) or "https://linear.app",
        sections=cap_sections(sections),
        mime="text/markdown",
        author=_name(node.get("creator")),
        created_at=when(node.get("createdAt")),
        updated_at=when(node.get("updatedAt")),
        metadata={"kind": "linear", "identifier": ident},
    )


def _name(raw: object) -> str | None:
    return text(obj(raw).get("name"))
