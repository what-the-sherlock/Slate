"""WorkerTrigger over GitHub's workflow_dispatch API (ADR-0035).

The worker is the GitHub Actions ``sync`` workflow. Its hourly cron is best effort: on a
quiet free repo GitHub starts it every 8 to 10 hours. When the API hands work to the
worker, this asks GitHub to start the workflow now.

* ``workflow_dispatch`` (Actions: write) rather than ``repository_dispatch`` (Contents:
  write): the token can start workflows but can't push code.
* Best effort: one attempt, a 5 s timeout, never raises. A missed nudge only means the
  work waits for the cron, as before. The workflow's concurrency group collapses bursts.
* Throttled per process: at most one dispatch per ``min_interval_s``, since each run wakes
  Neon for a few minutes (compute budget).
* Logs only the HTTP status and the reason label, never the token.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx
import structlog

from core.domain.github import is_valid_repo_name

log = structlog.get_logger(__name__)

TIMEOUT = httpx.Timeout(5.0, connect=3.0)
WORKFLOW = "sync.yml"


class GitHubWorkflowTrigger:
    def __init__(
        self,
        *,
        token: str,
        repo: str,
        ref: str = "main",
        min_interval_s: float = 120.0,
        client: httpx.AsyncClient | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not is_valid_repo_name(repo):
            raise ValueError("GITHUB_REPO must look like owner/name")
        self._token = token
        self._url = f"https://api.github.com/repos/{repo}/actions/workflows/{WORKFLOW}/dispatches"
        self._ref = ref
        self._min_interval = min_interval_s
        self._client = client
        self._monotonic = monotonic
        self._last: float | None = None

    async def trigger(self, reason: str) -> None:
        now = self._monotonic()
        if self._last is not None and now - self._last < self._min_interval:
            log.info("worker_trigger_throttled", reason=reason)
            return
        self._last = now
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            if self._client is not None:
                r = await self._client.post(
                    self._url, json={"ref": self._ref}, headers=headers, timeout=TIMEOUT
                )
            else:
                async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as c:
                    r = await c.post(self._url, json={"ref": self._ref}, headers=headers)
        except httpx.HTTPError as e:
            self._last = None  # let the next handoff try again
            log.warning("worker_trigger_failed", reason=reason, error=type(e).__name__)
            return
        if r.status_code == 204:
            log.info("worker_triggered", reason=reason)
            return
        self._last = None
        # 401/403: bad or under-scoped token; 404: wrong repo or workflow; 422: wrong ref.
        log.warning("worker_trigger_refused", reason=reason, status=r.status_code)


class NoWorkerTrigger:
    """No token configured: work waits for the cron, exactly as before."""

    async def trigger(self, reason: str) -> None:
        return None
