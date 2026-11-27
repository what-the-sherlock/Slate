"""Why isn't a connected source answering? A read-only health report of every source.

Prints, per source: connector, status, documents and chunks indexed, embedded chunks, the
OAuth connection's state, the latest sync runs (status, counts, skipped items, error
summary), waiting/failed/dead jobs and recent indexing errors; then whether the background
worker is keeping up, per job kind. It ends each source with plain hints (``hints``, pure
and unit-tested). It never prints document contents, titles of private documents, emails or
tokens.

The session is read-only (``default_transaction_read_only``), so it can't change data.

    uv run python scripts/diagnose_sources.py              # DATABASE_URL_DIRECT from .env
    uv run python scripts/diagnose_sources.py --connector github

Point DATABASE_URL_DIRECT at the database you want to inspect (e.g. production), in your
shell rather than in .env.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from core.adapters.postgres.engine import EngineKind, create_engine, use_selector_loop_on_windows
from core.config import get_settings
from core.domain.skips import describe_skips

CONNECTORS = (
    "upload", "github", "discord", "gmail_mbox", "imap",
    "gmail", "notion", "linear", "google_calendar", "google_drive", "slack",
)  # fmt: skip
RUN_STUCK = timedelta(minutes=10)
WORKER_OVERDUE = timedelta(hours=2)

_SOURCES = """
SELECT s.id, s.connector, s.name, s.status, s.data_class, s.last_synced_at,
       (s.checkpoint IS NOT NULL) AS has_checkpoint,
       c.status AS connection_status, left(c.last_error, 200) AS connection_error,
       (SELECT count(*) FROM documents d WHERE d.source_id = s.id AND d.deleted_at IS NULL) AS docs,
       (SELECT count(*) FROM chunks c JOIN documents d ON d.id = c.document_id
         WHERE d.source_id = s.id AND d.deleted_at IS NULL) AS chunks,
       (SELECT count(*) FROM chunk_embeddings e JOIN chunks c ON c.id = e.chunk_id
         JOIN documents d ON d.id = c.document_id
         WHERE d.source_id = s.id AND d.deleted_at IS NULL) AS embedded
FROM sources s
LEFT JOIN connections c ON c.id = s.connection_id
WHERE (CAST(:connector AS text) IS NULL OR s.connector = :connector)
ORDER BY s.created_at
"""
_RUNS = """
SELECT kind, status, docs_seen, docs_changed, docs_failed, docs_skipped, skipped,
       left(error_summary, 200) AS err, started_at, finished_at
FROM ingestion_runs WHERE source_id = :s ORDER BY created_at DESC LIMIT 3
"""
_JOBS = """
SELECT kind, status, attempts, max_attempts, run_after, locked_by, left(last_error, 200) AS err,
       created_at, finished_at
FROM jobs WHERE payload->>'source_id' = CAST(:s AS text)
ORDER BY created_at DESC LIMIT 5
"""
_ERRORS = """
SELECT e.stage, left(e.message, 160) AS message, count(*) AS n
FROM ingestion_errors e JOIN ingestion_runs r ON r.id = e.run_id
WHERE r.source_id = :s GROUP BY e.stage, left(e.message, 160) ORDER BY n DESC LIMIT 5
"""
# Per job kind: is anything handling it? The worker drains every runnable job each run.
_WORKER = """
SELECT kind,
       count(*) FILTER (WHERE status IN ('queued', 'failed') AND attempts < max_attempts
                        AND run_after < now() - interval '2 hours') AS overdue,
       count(*) FILTER (WHERE status IN ('queued', 'failed')) AS waiting,
       max(finished_at) FILTER (WHERE status = 'succeeded') AS last_succeeded,
       count(*) FILTER (WHERE status = 'dead') AS dead
FROM jobs GROUP BY kind ORDER BY kind
"""


def hints(
    src: Any,  # reason: database rows (attribute access); tests pass SimpleNamespace
    runs: Sequence[Any],
    jobs: Sequence[Any],
    *,
    now: datetime,
    overdue_kinds: Mapping[str, int] | None = None,
) -> list[str]:
    """Plain next steps for one source, newest evidence first. Pure."""
    out: list[str] = []
    latest_job = jobs[0] if jobs else None
    latest_run = runs[0] if runs else None
    job_broken = latest_job is not None and latest_job.status in {"failed", "dead"}

    if src.connection_status in {"needs_reconnect", "error"}:
        out.append(
            f"The account connection is {src.connection_status}:"
            f" {src.connection_error or 'no reason recorded'}. Reconnect it in Integrations."
        )
    if latest_job is not None and job_broken:
        verb = "gave up after" if latest_job.status == "dead" else "is retrying after"
        out.append(
            f"The latest {latest_job.kind} job {verb} {latest_job.attempts}/"
            f"{latest_job.max_attempts} attempts: {latest_job.err or 'no error recorded'}."
            + (" Press 'Sync now' to queue a fresh one." if latest_job.status == "dead" else "")
        )
        if (
            latest_run is not None
            and latest_run.status == "succeeded"
            and latest_run.docs_seen == 0
        ):
            out.append(
                "Its runs say 'succeeded, 0 seen': written before P0-1, when a crash was"
                " recorded as an empty success. The job's error above is the real cause."
            )
    waiting = [j for j in jobs if j.status in {"queued", "failed"}]
    if any(j.run_after is not None and now - j.run_after > WORKER_OVERDUE for j in waiting):
        kind = waiting[0].kind
        out.append(
            f"A {kind} job has been runnable for over 2 hours, so no worker handles {kind}."
            " Check the GitHub Actions 'sync' workflow runs hourly and is green, and that"
            " its logs have no '*_disabled' line (a missing secret turns that handler off)."
        )
    elif overdue_kinds and src.docs == 0 and any(overdue_kinds.values()):
        out.append("Other jobs are overdue (see Worker below): the worker may not be running.")
    if (
        latest_run is not None
        and latest_run.status == "running"
        and latest_run.started_at is not None
        and now - latest_run.started_at > RUN_STUCK
    ):
        out.append(
            "The latest run has been 'running' for over 10 minutes with no end: the process"
            " was killed mid-sync (e.g. the API function's time limit). Check Vercel's"
            " function max duration is at least 120 s."
        )
    if latest_run is not None and latest_run.status in {"failed", "partial"}:
        out.append(f"The latest run was {latest_run.status}: {latest_run.err or 'no summary'}")
    skipped = describe_skips(_skips(latest_run))
    if skipped:
        out.append(f"The latest run skipped {skipped}.")
    if src.docs and src.chunks == 0:
        out.append("Documents exist but have no chunks: parsing produced no text.")
    if src.chunks and src.embedded == 0:
        out.append("Chunks have no embeddings: only keyword search can find them.")
    if (
        src.connector == "github"
        and not job_broken
        and latest_run is not None
        and latest_run.status == "succeeded"
        and latest_run.docs_seen == 0
        and src.docs == 0
        and not skipped
    ):
        out.append(
            "The repo was read but held nothing Slate indexes (Markdown/text docs, README,"
            " issues and PRs; code is not indexed, ADR-0023)."
        )
    if src.docs == 0 and not runs and not jobs:
        out.append("Nothing was ever queued for this source: press 'Sync now'.")
    elif src.docs == 0 and not runs and jobs and not job_broken and not out:
        out.append("A sync is queued but hasn't run yet.")
    return out


def _skips(run: Any) -> dict[str, int]:
    raw = getattr(run, "skipped", None) if run is not None else None
    return dict(raw) if isinstance(raw, Mapping) else {}


async def run(connector: str | None) -> None:
    engine = create_engine(get_settings().require_database_url(direct=True), EngineKind.DIRECT)
    now = datetime.now(UTC)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SET default_transaction_read_only = on"))
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            worker = (await conn.execute(text(_WORKER))).all()
            overdue = {w.kind: int(w.overdue) for w in worker}
            sources = (await conn.execute(text(_SOURCES), {"connector": connector})).all()
            if not sources:
                print("No sources found.")
            for src in sources:
                runs = (await conn.execute(text(_RUNS), {"s": src.id})).all()
                jobs = (await conn.execute(text(_JOBS), {"s": src.id})).all()
                errors = (await conn.execute(text(_ERRORS), {"s": src.id})).all()
                # Repo names are the user's own; upload sources have fixed names.
                print(f"\n== {src.connector} | {src.name} | {src.status} | {src.data_class}")
                print(
                    f"   docs={src.docs} chunks={src.chunks} embedding_rows={src.embedded}"
                    f" last_synced={src.last_synced_at} checkpoint={src.has_checkpoint}"
                    + (f" connection={src.connection_status}" if src.connection_status else "")
                )
                for r in runs:
                    print(
                        f"   run  {r.kind:<11} {r.status:<9} seen={r.docs_seen}"
                        f" changed={r.docs_changed} failed={r.docs_failed}"
                        f" skipped={r.docs_skipped} at={r.started_at} {r.err or ''}"
                    )
                for j in jobs:
                    print(
                        f"   job  {j.kind:<11} {j.status:<9} attempts={j.attempts}/{j.max_attempts}"
                        f" run_after={j.run_after} {j.err or ''}"
                    )
                for e in errors:
                    print(f"   err  {e.stage:<6} x{e.n} {e.message}")
                for h in hints(src, runs, jobs, now=now, overdue_kinds=overdue):
                    print(f"   -> {h}")
            print("\n== Worker (all workspaces, counts only)")
            for w in worker:
                flag = "  <- NOT BEING PROCESSED" if w.overdue else ""
                print(
                    f"   {w.kind:<16} waiting={w.waiting} overdue_2h={w.overdue} dead={w.dead}"
                    f" last_succeeded={w.last_succeeded}{flag}"
                )
    finally:
        await engine.dispose()


def main() -> None:
    p = argparse.ArgumentParser(description="Read-only health report of every source.")
    p.add_argument("--connector", choices=CONNECTORS)
    args = p.parse_args()
    use_selector_loop_on_windows()
    asyncio.run(run(args.connector))


if __name__ == "__main__":
    main()
