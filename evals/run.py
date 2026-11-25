"""Run the retrieval evals (a composition root). See docs/EVALS.md.

    uv run python -m evals.run --embedder none      keyword lane only ($0, no API calls)
    uv run python -m evals.run --embedder openai    hybrid with text-embedding-3-small
    uv run python -m evals.run --embedder bge-m3    hybrid with BGE-M3 (run backfill first)

Writes evals/results/<date>_<git-sha>_<config>.json and a row in eval_runs, then prints
a summary. Exits non-zero if any guest leak is found (isolation must be exactly 0).

Cost: --embedder openai embeds the 51 questions once (~700 tokens, about $0.00002),
recorded in the usage ledger. bge-m3 sends only the synthetic questions to Cloudflare.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from core.adapters.factories import openai_embedder, workersai_embedder
from core.adapters.postgres.engine import (
    EngineKind,
    create_engine,
    create_session_factory,
    use_selector_loop_on_windows,
)
from core.adapters.postgres.evals import PostgresEvalStore
from core.adapters.postgres.search import PostgresSearchIndex
from core.adapters.postgres.uow import make_uow
from core.adapters.postgres.usage import PostgresGenerationStore, PostgresUsageLedger
from core.adapters.tokenizer import TiktokenTokenizer
from core.config import get_settings
from core.ports.models import Embedder
from core.ports.usage import UsageEntry
from core.services.search import VectorLane
from evals.cases import EXTERNAL_PREFIX, check_references, load_cases, load_corpus
from evals.harness import CachingEmbedder, run_cases
from evals.load import EVAL_WORKSPACE_NAME, EVAL_WORKSPACE_SLUG

RESULTS = Path(__file__).resolve().parent / "results"
OPENAI_USD_PER_M = Decimal("0.02")


def git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


async def run(embedder_name: str, config: str) -> int:
    settings = get_settings()
    cases = load_cases()
    corpus = load_corpus()
    check_references(cases, corpus)
    embedder: Embedder | None = None
    if embedder_name == "openai":
        embedder = openai_embedder(settings)
    elif embedder_name == "bge-m3":
        embedder = workersai_embedder(settings)
    if embedder_name != "none" and embedder is None:
        raise SystemExit(f"--embedder {embedder_name}: its API credentials aren't configured")

    engine = create_engine(settings.require_database_url(direct=True), EngineKind.DIRECT)
    try:
        uow = make_uow(create_session_factory(engine))
        store = PostgresEvalStore(uow)
        ws = await store.ensure_workspace(EVAL_WORKSPACE_SLUG, EVAL_WORKSPACE_NAME)
        ids = await store.document_ids(ws, [d.external_id for d in corpus])
        if len(ids) != len(corpus):
            raise SystemExit(
                "the eval corpus isn't fully loaded: run `uv run python -m evals.load`"
            )
        doc_ids = {ext.removeprefix(EXTERNAL_PREFIX): v for ext, v in ids.items()}

        lane: VectorLane | None = None
        cached: CachingEmbedder | None = None
        generation = None
        if embedder is not None:
            info = await PostgresGenerationStore(uow).find(embedder.model)
            if info is None:
                hint = "worker backfill" if embedder_name == "bge-m3" else "evals.load"
                raise SystemExit(f"no generation for {embedder.model}: run `{hint}` first")
            generation = info.id
            cached = CachingEmbedder(embedder)
            lane = VectorLane(cached, info.id)
            budget = Decimal(str(settings.monthly_llm_budget_usd))
            spent = await PostgresUsageLedger(uow).month_to_date_usd()
            if embedder_name == "openai" and spent >= budget:
                raise SystemExit("this month's AI budget is used up")

        report = await run_cases(
            PostgresSearchIndex(uow),
            ws,
            cases,
            doc_ids,
            await store.private_document_ids(ws),
            vector=lane,
        )

        if cached is not None and embedder_name == "openai":
            tokens = sum(TiktokenTokenizer().count(c.question) for c in cases)
            await PostgresUsageLedger(uow).record(
                UsageEntry(
                    workspace_id=ws,
                    provider="openai",
                    model=cached.model,
                    kind="embed",
                    data_class="private"
                    if any(c.visibility == "private" for c in cases)
                    else "public",
                    tokens_in=tokens,
                    tokens_out=0,
                    cost_usd=OPENAI_USD_PER_M * tokens / Decimal(1_000_000),
                )
            )

        sha = git_sha()
        run_config = {
            "name": config,
            "embedder": embedder_name,
            "model": embedder.model if embedder else None,
            "k": 10,
        }
        run_id = await store.record_run(
            git_sha=sha, generation=generation, config=run_config, metrics=report.summary
        )
    finally:
        await engine.dispose()

    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    out = RESULTS / f"{stamp}_{sha}_{config}.json"
    out.write_text(
        json.dumps(
            {"run_id": str(run_id), "git_sha": sha, "config": run_config, **report.as_json()},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print_summary(report.summary, out)
    leaks = report.summary["isolation"]["guest_leaks"]  # type: ignore[index]
    return 1 if leaks else 0


def print_summary(summary: dict[str, object], out: Path) -> None:
    overall = summary["overall"]
    print(f"\nOverall: {json.dumps(overall)}")
    print("By tag:")
    for tag, block in summary["by_tag"].items():  # type: ignore[union-attr]
        print(f"  {tag:<12} {json.dumps(block)}")
    print(f"By visibility: {json.dumps(summary['by_visibility'])}")
    print(f"Isolation: {json.dumps(summary['isolation'])}")
    print(f"Latency ms: {json.dumps(summary['latency_ms'])}")
    print(f"Answer gate: {json.dumps(summary.get('answer_gate'))}")
    print(f"\nWrote {out}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m evals.run")
    p.add_argument("--embedder", choices=["none", "openai", "bge-m3"], required=True)
    p.add_argument("--config", default=None, help="name for this configuration (default: embedder)")
    args = p.parse_args(argv)
    use_selector_loop_on_windows()
    return asyncio.run(run(args.embedder, args.config or args.embedder))


if __name__ == "__main__":
    raise SystemExit(main())
