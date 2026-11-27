"""The diagnosis script's hint rules (P0-3), with fake rows: no database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as Row

from scripts.diagnose_sources import CONNECTORS, hints

NOW = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)


def src(**kw: object) -> Row:
    base = {"connector": "github", "docs": 0, "chunks": 0, "embedded": 0,
            "connection_status": None, "connection_error": None}  # fmt: skip
    return Row(**{**base, **kw})


def run(status: str = "succeeded", seen: int = 0, **kw: object) -> Row:
    base = {"kind": "full", "status": status, "docs_seen": seen, "err": None,
            "started_at": NOW - timedelta(minutes=30), "skipped": {}}  # fmt: skip
    return Row(**{**base, **kw})


def job(status: str, **kw: object) -> Row:
    base = {"kind": "sync_github", "status": status, "attempts": 5, "max_attempts": 5,
            "err": "unexpected TypeError", "run_after": NOW - timedelta(minutes=5)}  # fmt: skip
    return Row(**{**base, **kw})


def test_every_connector_can_be_filtered() -> None:
    assert {"gmail", "google_drive", "imap", "slack", "notion"} <= set(CONNECTORS)


def test_a_dead_job_is_the_cause_never_an_empty_repo() -> None:
    # The production case: 3 runs "succeeded, seen=0" and a job dead after 5/5 TypeErrors.
    out = hints(src(), [run()], [job("dead")], now=NOW)
    assert any("gave up after 5/5 attempts: unexpected TypeError" in h for h in out)
    assert any("before P0-1" in h for h in out)
    assert not any("held nothing" in h for h in out)


def test_an_overdue_job_means_no_worker_handles_that_kind() -> None:
    late = job("queued", attempts=0, err=None, kind="sync_connected",
               run_after=NOW - timedelta(hours=3))  # fmt: skip
    out = hints(src(connector="gmail"), [], [late], now=NOW)
    assert any("no worker handles sync_connected" in h for h in out)


def test_a_run_stuck_in_running_was_killed() -> None:
    out = hints(src(), [run("running", started_at=NOW - timedelta(minutes=25))], [], now=NOW)
    assert any("killed mid-sync" in h for h in out)
    fresh = hints(src(), [run("running", started_at=NOW - timedelta(minutes=2))], [], now=NOW)
    assert not any("killed" in h for h in fresh)


def test_a_broken_connection_is_reported_with_its_reason() -> None:
    out = hints(src(connector="gmail", connection_status="needs_reconnect",
                    connection_error="invalid_grant"), [], [], now=NOW)  # fmt: skip
    assert out[0].startswith("The account connection is needs_reconnect: invalid_grant")


def test_skips_explain_an_empty_source() -> None:
    out = hints(src(connector="google_drive"), [run(skipped={"unsupported_type:pdf": 30})], [],
                now=NOW)  # fmt: skip
    assert "The latest run skipped 30 PDFs (not supported yet)." in out


def test_an_empty_repo_hint_only_when_it_really_was_read() -> None:
    out = hints(src(), [run()], [], now=NOW)
    assert any("held nothing" in h for h in out)


def test_never_queued() -> None:
    assert hints(src(), [], [], now=NOW) == [
        "Nothing was ever queued for this source: press 'Sync now'."
    ]
