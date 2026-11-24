# Evaluation

Retrieval quality is a feature with a number attached. No change to parsing, chunking, embedding, retrieval, ranking or prompts merges without an eval run that shows its effect.

## Dataset

`evals/questions.yaml`. The target is 50 questions by the end of Milestone 3 and 100 by the demo. Each case:

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

- `uv run python -m evals.run --embedder openai|bge-m3 --config <name>` writes `evals/results/<date>_<git-sha>_<config>.json` and a row in `eval_runs`.
- `eval.yml` is a manual or labelled workflow, never on every push. It uses the budget guard, and evals cost no more than $1 a month.
- Compare against the last `main` baseline. A regression of more than 2 points in Recall@10 blocks the change unless justified in the PR.
- The README's eval table is generated from the latest results.
