"""GitHubWorkflowTrigger (ADR-0035) over httpx.MockTransport: no network."""

from __future__ import annotations

import json

import httpx
import pytest

from core.adapters.github.dispatch import GitHubWorkflowTrigger


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make(
    status: int | Exception = 204,
) -> tuple[GitHubWorkflowTrigger, list[httpx.Request], Clock]:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if isinstance(status, Exception):
            raise status
        return httpx.Response(status)

    clock = Clock()
    trigger = GitHubWorkflowTrigger(
        token="github_pat_x", repo="owner/slate", min_interval_s=120,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), monotonic=clock,
    )  # fmt: skip
    return trigger, seen, clock


async def test_it_dispatches_the_sync_workflow_on_main() -> None:
    trigger, seen, _ = make()
    await trigger.trigger("sync_now:partial")
    [req] = seen
    assert req.method == "POST"
    assert req.url.path == "/repos/owner/slate/actions/workflows/sync.yml/dispatches"
    assert json.loads(req.content) == {"ref": "main"}
    assert req.headers["authorization"] == "Bearer github_pat_x"


async def test_bursts_are_throttled_then_allowed_again() -> None:
    trigger, seen, clock = make()
    await trigger.trigger("a")
    await trigger.trigger("b")  # within 120 s: one worker run drains both
    assert len(seen) == 1
    clock.t += 121
    await trigger.trigger("c")
    assert len(seen) == 2


@pytest.mark.parametrize("failure", [401, 404, 422, httpx.ConnectTimeout("slow")])
async def test_failures_never_raise_and_dont_block_the_next_try(
    failure: int | Exception,
) -> None:
    trigger, seen, _ = make(failure)
    await trigger.trigger("a")  # best effort: the cron still runs the work later
    await trigger.trigger("b")  # not throttled after a failure
    assert len(seen) == 2


def test_a_malformed_repo_is_refused_at_startup() -> None:
    with pytest.raises(ValueError, match="owner/name"):
        GitHubWorkflowTrigger(token="t", repo="https://github.com/owner/slate")
