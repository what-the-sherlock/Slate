"""Spike: does Neon's pgvector support what ARCHITECTURE §4/§8 relies on?

Checks, on the branch in DATABASE_URL_DIRECT (use the *test* branch):
  1. extension version (>= 0.8 needed for iterative index scans)
  2. halfvec(1024) + HNSW halfvec_cosine_ops
  3. recall of an ACL-filtered top-k with a selective filter, with iterative scan
     off vs relaxed_order, against exact (sequential) search.

Everything runs in a TEMP table, so nothing persists after the session.

    uv run python scripts/spike_pgvector.py [--rows 5000] [--selectivity 0.02]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from core.adapters.postgres.engine import (
    EngineKind,
    create_engine,
    use_selector_loop_on_windows,
)
from core.config import get_settings

DIMS = 1024
K = 10
QUERIES = 20


async def topk(conn: AsyncConnection, qid: int, *, mode: str) -> list[int]:
    """mode: exact | hnsw | iterative."""
    await conn.execute(text("SET LOCAL hnsw.ef_search = 40"))
    if mode == "exact":
        await conn.execute(text("SET LOCAL enable_indexscan = off"))
    else:
        # Force the HNSW path (what the planner picks once the visible set is large),
        # so we measure post-filter recall instead of the planner's exact fallback.
        await conn.execute(text("SET LOCAL enable_bitmapscan = off"))
        await conn.execute(text("SET LOCAL enable_seqscan = off"))
    if mode == "iterative":
        await conn.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    rows = await conn.execute(
        text(
            """
            SELECT id FROM spike_vec
            WHERE acl && ARRAY['user:me']::text[]
            ORDER BY embedding <=> (SELECT embedding FROM spike_q WHERE qid = :qid)
            LIMIT :k
            """
        ),
        {"qid": qid, "k": K},
    )
    return [r[0] for r in rows]


async def run(rows: int, selectivity: float) -> None:
    engine = create_engine(get_settings().require_database_url(direct=True), EngineKind.DIRECT)
    async with engine.connect() as conn:
        version = (
            await conn.execute(
                text("SELECT default_version FROM pg_available_extensions WHERE name='vector'")
            )
        ).scalar_one()
        pg = (await conn.execute(text("SHOW server_version"))).scalar_one()
        print(f"postgres {pg}; pgvector available: {version}")
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        installed = (
            await conn.execute(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))
        ).scalar_one()
        print(f"pgvector installed: {installed}")

        t = time.perf_counter()
        await conn.execute(
            text(  # f-string only inlines the int constant DIMS (typmods can't be bound)
                f"""
                CREATE TEMP TABLE spike_vec AS
                SELECT g AS id,
                       (SELECT array_agg(random() - 0.5) FROM generate_series(1, {DIMS})
                        WHERE g > 0)::halfvec({DIMS}) AS embedding,
                       CASE WHEN random() < :sel THEN ARRAY['user:me']
                            ELSE ARRAY['user:other'] END::text[] AS acl
                FROM generate_series(1, :rows) g
                """  # noqa: S608
            ),
            {"rows": rows, "sel": selectivity},
        )
        await conn.execute(
            text(  # f-string only inlines int constants DIMS and QUERIES
                f"""
                CREATE TEMP TABLE spike_q AS
                SELECT q AS qid,
                       (SELECT array_agg(random() - 0.5) FROM generate_series(1, {DIMS})
                        WHERE q > 0)::halfvec({DIMS}) AS embedding
                FROM generate_series(1, {QUERIES}) q
                """  # noqa: S608
            )
        )
        visible = (
            await conn.execute(text("SELECT count(*) FROM spike_vec WHERE acl && ARRAY['user:me']"))
        ).scalar_one()
        print(
            f"generated {rows} halfvec({DIMS}) rows, {visible} visible, "
            f"in {time.perf_counter() - t:.1f}s"
        )

        t = time.perf_counter()
        await conn.execute(
            text("CREATE INDEX spike_hnsw ON spike_vec USING hnsw (embedding halfvec_cosine_ops)")
        )
        await conn.execute(text("CREATE INDEX spike_acl ON spike_vec USING gin (acl)"))
        await conn.execute(text("ANALYZE spike_vec"))
        print(f"HNSW (halfvec_cosine_ops) + GIN built in {time.perf_counter() - t:.1f}s")
        await conn.commit()

        plan = await conn.execute(
            text(
                """
                EXPLAIN SELECT id FROM spike_vec WHERE acl && ARRAY['user:me']::text[]
                ORDER BY embedding <=> (SELECT embedding FROM spike_q WHERE qid = 1) LIMIT 10
                """
            )
        )
        uses_hnsw = any("spike_hnsw" in r[0] for r in plan)
        await conn.commit()
        print(f"planner picks HNSW unforced for this filter: {uses_hnsw} (False = exact via GIN)")

        results: dict[str, list[float]] = {"hnsw": [], "iterative": []}
        returned: dict[str, list[int]] = {"hnsw": [], "iterative": []}
        for qid in range(1, QUERIES + 1):
            async with conn.begin():
                exact = set(await topk(conn, qid, mode="exact"))
            for mode in ("hnsw", "iterative"):
                async with conn.begin():
                    got = await topk(conn, qid, mode=mode)
                returned[mode].append(len(got))
                results[mode].append(len(exact & set(got)) / max(len(exact), 1))

        for mode in ("hnsw", "iterative"):
            recall = sum(results[mode]) / len(results[mode])
            avg_n = sum(returned[mode]) / len(returned[mode])
            print(f"{mode:9s}: recall@{K} = {recall:.2f}, rows returned avg {avg_n:.1f}/{K}")
    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=5000)
    parser.add_argument("--selectivity", type=float, default=0.02)
    args = parser.parse_args()
    use_selector_loop_on_windows()
    asyncio.run(run(args.rows, args.selectivity))


if __name__ == "__main__":
    main()
