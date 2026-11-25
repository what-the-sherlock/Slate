"""Why isn't a connected source answering? A read-only health report of every source.

Prints, per source: connector, status, documents and chunks indexed, embedded chunks, the
latest sync run (status, counts, error summary), waiting/failed jobs and recent indexing
errors. It never prints document contents, titles of private documents, emails or tokens.

The session is read-only (``default_transaction_read_only``), so it can't change data.

    uv run python scripts/diagnose_sources.py              # DATABASE_URL_DIRECT from .env
    uv run python scripts/diagnose_sources.py --connector github

Point DATABASE_URL_DIRECT at the database you want to inspect (e.g. production).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from core.adapters.postgres.engine import EngineKind, create_engine, use_selector_loop_on_windows
from core.config import get_settings

_SOURCES = """
SELECT s.id, s.connector, s.name, s.status, s.data_class, s.last_synced_at,
       (s.checkpoint IS NOT NULL) AS has_checkpoint,
       (SELECT count(*) FROM documents d WHERE d.source_id = s.id AND d.deleted_at IS NULL) AS docs,
       (SELECT count(*) FROM chunks c JOIN documents d ON d.id = c.document_id
         WHERE d.source_id = s.id AND d.deleted_at IS NULL) AS chunks,
       (SELECT count(*) FROM chunk_embeddings e JOIN chunks c ON c.id = e.chunk_id
         JOIN documents d ON d.id = c.document_id
         WHERE d.source_id = s.id AND d.deleted_at IS NULL) AS embedded
FROM sources s
WHERE (CAST(:connector AS text) IS NULL OR s.connector = :connector)
ORDER BY s.created_at
"""
_RUNS = """
SELECT kind, status, docs_seen, docs_changed, docs_failed, left(error_summary, 200) AS err,
       started_at, finished_at
FROM ingestion_runs WHERE source_id = :s ORDER BY created_at DESC LIMIT 3
"""
_JOBS = """
SELECT kind, status, attempts, max_attempts, run_after, left(last_error, 200) AS err, created_at
FROM jobs WHERE payload->>'source_id' = CAST(:s AS text)
  AND status IN ('queued', 'running', 'failed', 'dead')
ORDER BY created_at DESC LIMIT 5
"""
_ERRORS = """
SELECT e.stage, left(e.message, 160) AS message, count(*) AS n
FROM ingestion_errors e JOIN ingestion_runs r ON r.id = e.run_id
WHERE r.source_id = :s GROUP BY e.stage, left(e.message, 160) ORDER BY n DESC LIMIT 5
"""


def _hints(src: Any, runs: Sequence[Any], jobs: Sequence[Any]) -> list[str]:  # reason: row values
    hints: list[str] = []
    if src.docs == 0 and not runs and jobs:
        hints.append(
            "A sync is queued but never ran: no worker is running this job kind. Check the"
            " worker's GitHub App and OPENAI_API_KEY secrets (it logs github_sync_disabled),"
            " or press 'Sync now' on a server where GitHub is configured."
        )
    if src.docs == 0 and not runs and not jobs:
        hints.append("Nothing was ever queued for this source: press 'Sync now'.")
    if runs and runs[0].status in {"failed", "partial"}:
        hints.append(f"The latest run was {runs[0].status}: {runs[0].err or 'no summary'}")
    if src.docs and src.chunks == 0:
        hints.append("Documents exist but have no chunks: parsing produced no text.")
    if src.chunks and src.embedded == 0:
        hints.append("Chunks have no embeddings: only keyword search can find them.")
    if src.connector == "github" and runs and runs[0].docs_seen == 0 and src.docs == 0:
        hints.append(
            "The repo was read but held nothing Slate indexes (Markdown/text docs, README,"
            " issues and PRs; code is not indexed, ADR-0023)."
        )
    return hints


async def run(connector: str | None) -> None:
    engine = create_engine(get_settings().require_database_url(direct=True), EngineKind.DIRECT)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SET default_transaction_read_only = on"))
            await conn.execute(text("SET TRANSACTION READ ONLY"))
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
                )
                for r in runs:
                    print(
                        f"   run  {r.kind:<11} {r.status:<9} seen={r.docs_seen}"
                        f" changed={r.docs_changed} failed={r.docs_failed}"
                        f" at={r.started_at} {r.err or ''}"
                    )
                for j in jobs:
                    print(
                        f"   job  {j.kind:<11} {j.status:<9} attempts={j.attempts}/{j.max_attempts}"
                        f" run_after={j.run_after} {j.err or ''}"
                    )
                for e in errors:
                    print(f"   err  {e.stage:<6} x{e.n} {e.message}")
                for h in _hints(src, runs, jobs):
                    print(f"   -> {h}")
    finally:
        await engine.dispose()


def main() -> None:
    p = argparse.ArgumentParser(description="Read-only health report of every source.")
    p.add_argument("--connector", choices=["upload", "github", "discord", "gmail_mbox"])
    args = p.parse_args()
    use_selector_loop_on_windows()
    asyncio.run(run(args.connector))


if __name__ == "__main__":
    main()
