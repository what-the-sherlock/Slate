"""The status badge (P0-2): never healthy when something is wrong, always says why."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.domain.source_status import (
    INTERRUPTED,
    WAITING_FOR_WORKER,
    Badge,
    SourceFacts,
    source_status,
)

NOW = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)
AGO = {m: NOW - timedelta(minutes=m) for m in (1, 5, 20, 45, 90, 180, 600)}


def facts(**kw: object) -> SourceFacts:
    base: dict[str, object] = {
        "status": "active",
        "documents": 4,
        "last_synced_at": AGO[5],
        "run_status": "succeeded",
        "run_started_at": AGO[5],
    }
    base.update(kw)
    return SourceFacts(**base)  # type: ignore[arg-type]


def test_a_healthy_source_is_synced() -> None:
    st = source_status(facts(), NOW)
    assert (st.badge, st.healthy, st.busy) == (Badge.SYNCED, True, False)


def test_paused_wins_over_everything() -> None:
    assert source_status(facts(status="paused", jobs_waiting=3), NOW).badge is Badge.PAUSED


def test_a_running_job_is_syncing_until_it_is_clearly_stuck() -> None:
    assert source_status(facts(jobs_running=1, running_since=AGO[1]), NOW).badge is Badge.SYNCING
    st = source_status(facts(jobs_running=1, running_since=AGO[45]), NOW)
    assert (st.badge, st.reason) == (Badge.WAITING_FOR_WORKER, WAITING_FOR_WORKER)


def test_a_failed_job_awaiting_retry_says_retrying_with_its_error() -> None:
    # The owner's screen showed "Queued" here, with no end (P0-2).
    st = source_status(
        facts(jobs_waiting=1, oldest_waiting_due=AGO[5], retry_error="unexpected KeyError"), NOW
    )
    assert st.badge is Badge.RETRYING
    assert st.reason == "Retrying: Slate hit an internal error (KeyError)."
    assert st.busy
    assert not st.healthy


@pytest.mark.parametrize("retry_error", [None, "unexpected TypeError"])
def test_work_no_worker_picks_up_is_waiting_for_the_worker(retry_error: str | None) -> None:
    st = source_status(
        facts(jobs_waiting=1, oldest_waiting_due=AGO[180], retry_error=retry_error), NOW
    )
    assert st.badge is Badge.WAITING_FOR_WORKER
    assert st.reason
    assert st.reason.startswith(WAITING_FOR_WORKER)
    assert not st.busy  # not counted in "Processing N sources"


def test_a_retry_scheduled_in_the_future_is_not_overdue() -> None:
    st = source_status(
        facts(
            jobs_waiting=1,
            oldest_waiting_due=NOW + timedelta(minutes=8),
            retry_error="GitHub is unavailable right now.",
        ),
        NOW,
    )
    assert st.badge is Badge.RETRYING


def test_freshly_queued_is_queued() -> None:
    st = source_status(facts(jobs_waiting=1, oldest_waiting_due=AGO[1]), NOW)
    assert (st.badge, st.busy) == (Badge.QUEUED, True)


def test_a_dead_job_after_bogus_successes_is_a_failure() -> None:
    # Production before P0-1: runs said "succeeded, 0 seen" and the job died 5 of 5 times.
    st = source_status(
        facts(
            documents=0,
            last_ok_run_at=AGO[90],
            dead_at=AGO[20],
            dead_error="unexpected TypeError",
        ),
        NOW,
    )
    assert st.badge is Badge.FAILED
    assert st.reason == "Stopped after repeated failures: Slate hit an internal error (TypeError)."


def test_a_dead_job_older_than_a_good_run_no_longer_matters() -> None:
    st = source_status(facts(last_ok_run_at=AGO[5], dead_at=AGO[600], dead_error="x"), NOW)
    assert st.badge is Badge.SYNCED


def test_a_run_left_running_without_a_job_was_interrupted() -> None:
    st = source_status(facts(run_status="running", run_started_at=AGO[20]), NOW)
    assert (st.badge, st.reason) == (Badge.FAILED, INTERRUPTED)
    fresh = source_status(facts(run_status="running", run_started_at=AGO[1]), NOW)
    assert fresh.badge is Badge.SYNCING


@pytest.mark.parametrize(
    ("run", "badge"),
    [("failed", Badge.FAILED), ("partial", Badge.PARTIAL), ("cancelled", Badge.PARTIAL)],
)
def test_the_last_run_decides_when_nothing_is_pending(run: str, badge: Badge) -> None:
    st = source_status(facts(run_status=run, run_error="why"), NOW)
    assert (st.badge, st.reason) == (badge, "why")


def test_an_empty_source_is_never_shown_as_healthy() -> None:
    st = source_status(facts(documents=0, run_skipped={"unsupported_type:pdf": 30}), NOW)
    assert st.badge is Badge.EMPTY
    assert st.reason == "Skipped 30 PDFs (not supported yet)."
    assert not st.healthy
    assert source_status(facts(documents=0), NOW).reason == "Nothing to index was found."


def test_never_synced() -> None:
    assert source_status(facts(last_synced_at=None, run_status=None), NOW).badge is Badge.NEVER


def test_leftover_retries_older_than_a_good_run_are_not_the_headline() -> None:
    # Production 2026-11-27: four retries of the pre-fix TypeError waited for a worker
    # while the deployed fix had already synced the repo. Show the good run.
    st = source_status(
        facts(jobs_waiting=4, oldest_waiting_due=AGO[600], newest_waiting_due=AGO[180],
              retry_error="unexpected TypeError", last_ok_run_at=AGO[5]),
        NOW,
    )  # fmt: skip
    assert st.badge is Badge.SYNCED


def test_a_retry_due_after_the_last_good_run_still_counts() -> None:
    st = source_status(
        facts(jobs_waiting=1, oldest_waiting_due=AGO[180], newest_waiting_due=AGO[180],
              retry_error="unexpected TypeError", last_ok_run_at=AGO[600]),
        NOW,
    )  # fmt: skip
    assert st.badge is Badge.WAITING_FOR_WORKER
