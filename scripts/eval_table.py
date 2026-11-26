"""Regenerate the README's eval table from stored results (EVALS.md: "The README's eval table
is generated from the latest results").

    uv run python scripts/eval_table.py          print the table
    uv run python scripts/eval_table.py --write  replace it in README.md (between markers)

Reads evals/results/*.json only. No network, no database, no spend.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "evals" / "results"
README = ROOT / "README.md"
START, END = "<!-- eval-table:start -->", "<!-- eval-table:end -->"


def _load(path: Path) -> dict[str, Any] | None:
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _num(value: Any, digits: int = 3) -> str:
    return f"{value:.{digits}f}" if isinstance(value, int | float) else "—"


def table() -> str:
    retrieval: dict[str, tuple[str, dict[str, Any]]] = {}  # embedder -> newest (file, data)
    answers: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(RESULTS.glob("*.json")):
        data = _load(path)
        if data is None:
            continue
        if "model" in data and "summary" in data and "abstention_accuracy" in data["summary"]:
            answers.append((path.stem, data))
            continue
        cfg = data.get("config") or {}
        embedder = str(cfg.get("embedder") or path.stem.rsplit("_", 1)[-1])
        retrieval[embedder] = (path.stem, data)  # sorted by name = date: last one wins

    lines = [
        "| Retrieval (hybrid unless noted) | Cases | Recall@10 | MRR@10 | nDCG@10"
        " | Outsider leaks | Run |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for embedder, (stem, data) in sorted(retrieval.items()):
        s = data.get("summary") or {}
        o = s.get("overall") or {}
        leaks = (s.get("isolation") or {}).get("guest_leaks", "—")
        label = "keyword only" if embedder == "none" else embedder
        lines.append(
            f"| {label} | {o.get('cases', '—')} | {_num(o.get('recall@10'))} | "
            f"{_num(o.get('mrr@10'))} | {_num(o.get('ndcg@10'))} | {leaks} | `{stem}` |"
        )
    if answers:
        lines += [
            "",
            "| Answers | Cases | Abstention accuracy | Citation validity"
            " | Injection followed | Cost | Run |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for stem, data in answers:
            s = data["summary"]
            lines.append(
                f"| {data.get('model', '?')} | {s.get('cases', '—')} | "
                f"{_num(s.get('abstention_accuracy'))} | {_num(s.get('citation_validity'))} | "
                f"{len(s.get('followed_injection') or [])} | ${_num(s.get('cost_usd'), 4)}"
                f" | `{stem}` |"
            )
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--write", action="store_true", help="replace the table in README.md")
    args = p.parse_args()
    out = table()
    if not args.write:
        print(out)
        return 0
    text = README.read_text(encoding="utf-8")
    if START not in text or END not in text:
        print("README.md has no eval-table markers", file=sys.stderr)
        return 1
    head, rest = text.split(START, 1)
    _old, tail = rest.split(END, 1)
    README.write_text(f"{head}{START}\n{out}\n{END}{tail}", encoding="utf-8", newline="\n")
    print("README.md eval table updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
