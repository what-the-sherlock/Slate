# Evaluation

Retrieval quality is a feature with a number attached. No change to parsing, chunking, embedding, retrieval, ranking or prompts merges without an eval run that shows its effect.

## Dataset

`evals/questions.yaml` (51 cases as of M2) over the synthetic corpus in `evals/corpus/`: a fictional company, 14 public and 4 private Markdown documents with front matter (`title`, `visibility`). The target is 100 cases by the demo. `expected_documents` are corpus paths. Each case:

```yaml
- id: gh-auth-001
  question: "How does the API authenticate requests?"
  expected_documents:        # external_ids or URIs, stable across re-ingestion
    - "github:aashu/slate:docs/SECURITY.md"
  expected_answer_points:    # optional, for answer checks
    - "session cookie"
    - "CSRF header"
  tags: [github, docs, single-hop]
  visibility: public         # public | private; private cases run only with the owner's principals
```

Mix the question types: single-hop lookup, multi-hop (two sources), follow-up (with history), keyword-heavy (identifiers, error codes), "no answer exists" (must trigger insufficient evidence), and guest-isolation (the guest must not get the private answer).

## Metrics

| Metric | What | Target for demo |
| --- | --- | --- |
| Recall@10 | expected doc in the top 10 fused results | ≥ 0.85 |
| MRR@10 | rank quality | track |
| nDCG@10 | graded ranking | track |
| Citation validity | share of `[n]` that point to a retrieved chunk supporting the claim | ≥ 0.95 |
| Abstention accuracy | "no answer" cases correctly abstain | ≥ 0.9 |
| Isolation | guest leakage on private cases | exactly 0 |
| Latency p50/p95 | retrieval and time-to-first-token | track |
| Cost per question | from `usage_ledger` | track |

Retrieval metrics are computed without an LLM. Answer metrics use point coverage (string and semantic match) and, optionally, an LLM judge. The judge runs only when the budget allows, on a sample, and is clearly labelled.

## Process

- The corpus lives in its own workspace (`slate-evals`, ADR-0020), never in a real one. Load it with `uv run python -m evals.load` (idempotent by checksum; the first load embeds ~6k tokens with OpenAI, about $0.0001).
- `uv run python -m evals.run --embedder none|openai|bge-m3 [--config <name>]` writes `evals/results/<date>_<git-sha>_<config>.json` and a row in `eval_runs`. `none` is the keyword lane alone and costs nothing. `bge-m3` needs `uv run python -m worker backfill` first (public chunks only, ADR-0012).
- Every case also runs as a guest; a private document in a guest's results is a leak, and the run exits non-zero.
- Retrieval goes through `hybrid_retrieve`, the same function the Search API uses, so the numbers are what users get. Metrics are computed over unique documents in the top 10 chunks.
- `eval.yml` is a manual or labelled workflow, never on every push. It uses the budget guard, and evals cost no more than $1 a month.
- Compare against the last `main` baseline. A regression of more than 2 points in Recall@10 blocks the change unless justified in the PR.
- The README's eval table is generated from the latest results.

## Results

| Date | Config | Recall@10 | MRR@10 | nDCG@10 | Guest leaks | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-11-25 | keyword, AND (`websearch_to_tsquery`) | 0.70 on `keyword` cases | | | 0 | Every word had to match; "what does KF-5002 mean" missed |
| 2026-11-25 | keyword, any word (ADR-0019) | 0.936 (47 cases) | 0.654 | 0.714 | 0 | Misses are pure paraphrases: hb-exp-003, prod-price-002, prod-sla-001 |
| 2026-11-25 | hybrid, OpenAI `text-embedding-3-small` (`00e96d3`) | **1.000** (47) | **0.930** | **0.947** | 0 | Every expected document in the top 10; all 6 private cases rank 1. Retrieval p50 2.0 s from the dev laptop (India → Neon us-east-1, ~10 round trips; not representative of Vercel) |
| 2026-11-25 | hybrid, BGE-M3 (`00e96d3`, generation 111, public-scoped) | 0.894 (47) | 0.833 | 0.845 | 0 | Public: R 1.000, MRR 0.943 (slightly above OpenAI's 0.920). Private: R **0.167**, below keyword-only. See "Findings" |

### Findings

1. **Hybrid beats keyword-only by a wide margin** on paraphrased questions (MRR 0.654 → 0.930 with OpenAI). The vector lane is pulling its weight.
2. **On public data BGE-M3 matches OpenAI** (Recall 1.000 for both; MRR 0.943 vs 0.920). On this small corpus the free model is not worse.
3. **A lane that *can't* see a document drags it down in RRF.** BGE-M3's generation is public-scoped (ADR-0012), so private chunks exist only in the keyword lane. RRF gives a keyword-only rank-1 hit 1/61 ≈ 0.016, while public chunks found by *both* lanes at modest ranks score ~2/65 ≈ 0.031, so 5 of 6 private documents fell out of the top 10, although keyword-only search found them all. RRF assumes absence from a lane means "not relevant"; here it means "not allowed in this lane".
4. **Decision:** OpenAI stays the `present` generation; BGE-M3 is not flipped. If a public-scoped generation is ever served, the fix is to fuse the vector lane only over documents it covers (or run private data keyword + OpenAI), and re-run this table first.

The keyword rows were measured by `tests/integration/evals/test_eval_corpus_pg.py` (real Neon, same harness, fake vectors so no API cost). Replace the pending rows with `evals.run` output.
