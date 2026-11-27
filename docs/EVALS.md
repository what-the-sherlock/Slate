# Evaluation

Retrieval quality is a feature with a number attached. No change to parsing, chunking, embedding, retrieval, ranking or prompts merges without an eval run that shows its effect.

## Dataset

`evals/questions.yaml` (84 cases as of M8) over the synthetic corpus in `evals/corpus/`: a fictional company, Kestrel Freight. It has 14 public and 4 private Markdown documents, plus (M8, ADR-0031) 14 private **connector-shaped** documents: email threads (`email/`), Discord windows (`discord/`) and GitHub issues and PRs (`github/`), written in the text shape those connectors produce. Front matter: `title`, `visibility`, and for connector documents `kind` (`email|discord|github`), `author` and `date` (ISO 8601). `evals.load` puts each kind under a source with its connector (`imap`, `discord`, `github`) and sets the author and date, so source-kind, sender and date filters work exactly as in production. Connector documents are dated around the eval world's today, **Friday 27 November 2026** (`EVAL_NOW` in `evals/cases.py`). The target is 100 cases. `expected_documents` are corpus paths. Each case:

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

An optional `plan` is an **oracle plan** (ADR-0031): what a correct planner would return (`intent`, `after`, `before`, `sources`, `sender`). The free retrieval eval (`evals.run`) applies it, measuring the recent lane and filters given a correct plan. The paid answer eval ignores it and runs the real planner under the fixed clock, so its numbers include planner mistakes.

```yaml
- id: conn-period-001
  question: Summarise the emails I got last week.
  expected_documents: [email/acme-invoice.md, email/nordlys-escalation.md]
  tags: [connector, summary, email, period]
  visibility: private
  plan: {intent: summary, after: 2026-11-16, before: 2026-11-22, sources: [email]}
```

**Hard sets (IMPROVEMENT_PLAN P1-8, 118 cases, 73 documents).** Retrieval was saturated (Recall@10 = 1.000), so these were added to make regressions visible:

| Tag | Cases | What it asserts |
| --- | --- | --- |
| `multi-turn` | 10 | `history` holds earlier turns (`question`, `cited_documents`). The follow-up shares no keywords with what it needs. `evals.run` runs it alone, which measures follow-up anchoring (P1-5); `evals.answers` really asks the earlier turns in one chat |
| `cross-source` | 8 | `min_cited_kinds: 2`: the answer must cite two corpus kinds (`cross_source_ok`) |
| `conflict` | 5 | a newer connector document overrides an older file; the point is the newer value |
| `temporal` | 5 | periods relative to `EVAL_NOW`, with oracle plans |
| `title-only` | 6 | the words asked for appear only in the title (a unit test keeps them out of the body); measured before and after P1-3 |
| distractors | 30 docs | `archive/` (stale near-duplicates with old numbers) and `misc/` (off-topic) |

Sync coverage is tested per engine with $0 contract tests (a crash is a `failed` run; skips are counted), in `tests/unit/core/services/test_{github,discord,mail,connected,sync_run}.py`.

Mix the question types: single-hop lookup, multi-hop (two sources), follow-up (with history), keyword-heavy (identifiers, error codes), "no answer exists" (must trigger insufficient evidence), and isolation (an outsider, entitled to public content only, must not get the private answer).

## Metrics

| Metric | What | Target for demo |
| --- | --- | --- |
| Recall@10 | expected doc in the top 10 fused results | ≥ 0.85 |
| MRR@10 | rank quality | track |
| nDCG@10 | graded ranking | track |
| Citation validity | share of `[n]` that point to a retrieved chunk supporting the claim | ≥ 0.95 |
| Abstention accuracy | "no answer" cases correctly abstain | ≥ 0.9 |
| Isolation | outsider ("guest") leakage on private cases | exactly 0 |
| Latency p50/p95 | retrieval and time-to-first-token | track |
| Cost per question | from `usage_ledger` | track |

Retrieval metrics are computed without an LLM. Answer metrics use point coverage (string and semantic match) and, optionally, an LLM judge. The judge runs only when the budget allows, on a sample, and is clearly labelled.

## Process

- The corpus lives in its own workspace (`slate-evals`, ADR-0020), never in a real one. Load it with `uv run python -m evals.load` (idempotent by checksum; the first load embeds ~6k tokens with OpenAI, about $0.0001).
- `uv run python -m evals.run --embedder none|openai|bge-m3 [--config <name>]` writes `evals/results/<date>_<git-sha>_<config>.json` and a row in `eval_runs`. `none` is the keyword lane alone and costs nothing. `bge-m3` needs `uv run python -m worker backfill` first (public chunks only, ADR-0012).
- Every case also runs as an outsider (`Principals.for_outsider`, reported as "guest" in results; the guest login itself is gone, ADR-0021); a private document in its results is a leak, and the run exits non-zero.
- Retrieval goes through `hybrid_retrieve`, the same function the Search API uses, so the numbers are what users get. Metrics are computed over unique documents in the top 10 chunks.
- `eval.yml` is a manual or labelled workflow, never on every push. It uses the budget guard, and evals cost no more than $1 a month.
- Compare against the last `main` baseline. A regression of more than 2 points in Recall@10 blocks the change unless justified in the PR.
- The README's eval table is generated from the latest results.

## Answer evals (M3, paid)

`uv run python -m evals.answers [--limit N]` runs the real `AnswerService` (gpt-4o-mini by default) over the cases. It costs about $0.001 a case, and refuses to run if the global caps couldn't cover it. Chats stay in memory. It reports:
- **abstention accuracy:** no-answer cases must abstain, answerable cases must not; with false and missed abstentions listed. Cases with `expect_mode` (`gen-*` general knowledge, `chat-*` small talk; ADR-0025) must *not* abstain
- **mode accuracy:** the share of `expect_mode` cases answered in that mode (`wrong_mode` lists the misses), and `general_instead_of_grounded`, answerable cases the model answered from general knowledge instead of the sources (should be empty)
- **citation validity:** share of cited documents among the expected ones
- **point coverage:** expected answer points found in the text
- **answers that followed the injection fixture:** must be none
- **cross-source:** `cross_source_ok`, the share of `min_cited_kinds` cases citing enough kinds, and `cross_source_failed`
- **with `--judge` only** (`evals/judge.py`, one extra gpt-4o-mini call per answer, about $0.0005): **faithfulness** (cited sentences that their passages support) and **citation completeness** (factual sentences that cite). The judge sees only the cited passages, numbered as in the answer, and never the system prompt's sources. An unparseable verdict leaves the answer unscored, not guessed

The free retrieval run (`evals.run`) also reports `answer_gate`: the lowest best-similarity among answerable cases, the highest among no-answer cases, and a suggested `CHAT_MIN_SIMILARITY` between them (ADR-0022). Re-tune it whenever the embedder changes.

## Results

| Date | Config | Recall@10 | MRR@10 | nDCG@10 | Guest leaks | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-11-25 | keyword, AND (`websearch_to_tsquery`) | 0.70 on `keyword` cases | | | 0 | Every word had to match; "what does KF-5002 mean" missed |
| 2026-11-25 | keyword, any word (ADR-0019) | 0.936 (47 cases) | 0.654 | 0.714 | 0 | Misses are pure paraphrases: hb-exp-003, prod-price-002, prod-sla-001 |
| 2026-11-25 | hybrid, OpenAI `text-embedding-3-small` (`00e96d3`) | **1.000** (47) | **0.930** | **0.947** | 0 | Every expected document in the top 10; all 6 private cases rank 1. Retrieval p50 2.0 s from the dev laptop (India → Neon us-east-1, ~10 round trips; not representative of Vercel) |
| 2026-11-25 | hybrid, BGE-M3 (`00e96d3`, generation 111, public-scoped) | 0.894 (47) | 0.833 | 0.845 | 0 | Public: R 1.000, MRR 0.943 (slightly above OpenAI's 0.920). Private: R **0.167**, below keyword-only. See "Findings" |
| 2026-11-25 | hybrid, OpenAI, +injection doc (`fa7e091`, 49 answerable + 4 no-answer) | **1.000** (49) | 0.930 | 0.947 | 0 | Injection cases R 1.000, MRR 1.000. Answer gate: answerable min similarity 0.245 (kw-007), no-answer max 0.353 (na-002): they overlap, so no clean cut. `CHAT_MIN_SIMILARITY` set to 0.20 (no false abstentions; gates na-004 for free), and the model sentinel handles the rest (ADR-0022) |
| 2026-11-25 | **answers**, gpt-4o-mini (`fa7e091`, 53 cases, `evals.answers`) | | | | 0 | Abstention accuracy **0.943** (0 missed; 3 false: prod-road-001, a fact under a "Q2 2027" heading; kw-005 and kw-006, bare keywords). Citation validity **1.000**. Point coverage 0.976 (re-scored with the word-overlap matcher; 0.768 with exact substrings). Injection followed: none. $0.0083 total; p50 10.3 s end to end from the laptop. Prompt then updated for headings and keywords; re-check with `--ids prod-road-001,kw-005,kw-006` |

| 2026-11-27 | keyword, M8 (`ffae766`, 84 cases, oracle plans) | 0.919 (74) | 0.600 | 0.668 | 0 | Connector, recent, period and sender tags all R 1.000: the recent lane and filters find dated documents with no words in common |
| 2026-11-27 | hybrid, OpenAI, M8 (`ffae766`, 84 cases, oracle plans) | **1.000** (74) | 0.904 | 0.924 | 0 | Every tag R 1.000. Answer gate: answerable min 0.2445, no-answer max 0.389 (overlap; 0.20 kept, no false gate abstentions) |
| 2026-11-27 | **answers**, gpt-4o-mini + planner (`ffae766`, 84 cases) | | | | 0 | Abstention 0.952 (0 missed; 4 false: conn-period-002, conn-sender-003, conn-email-003, conn-kw-001). Mode 1.000. Citation validity 0.986. Points 0.905. Injection followed: none. kw-004 answered from general knowledge instead of its source. $0.035; p50 12.9 s. conn-period-001 read "last week" as the past 7 days: fixed by computed calendar ranges in the planner prompt. Re-run the 6 misses after that fix (the run now records each plan and the sources shown) |
| 2026-11-27 | **answers**, the 6 misses re-run after the calendar fix | | | | 0 | 4 of 6 now pass (conn-period-001 summarises 16-22 Nov). The other 2 had the same cause, visible in the recorded plans: the planner set `sources: github` on questions that never named GitHub (conn-sender-003 "ledger latency", kw-004 "kfctl rollback"), so the hard filter hid the Discord and file answers but still returned some GitHub documents, and the empty-result retry never fired. Fixed in code: a source kind survives only if the question's own words name it (`names_kind`). Re-run these 2 to confirm |

### Findings

1. **Hybrid beats keyword-only by a wide margin** on paraphrased questions (MRR 0.654 → 0.930 with OpenAI). The vector lane is pulling its weight.
2. **On public data BGE-M3 matches OpenAI** (Recall 1.000 for both; MRR 0.943 vs 0.920). On this small corpus the free model is not worse.
3. **A lane that *can't* see a document drags it down in RRF.** BGE-M3's generation is public-scoped (ADR-0012), so private chunks exist only in the keyword lane. RRF gives a keyword-only rank-1 hit 1/61 ≈ 0.016, while public chunks found by *both* lanes at modest ranks score ~2/65 ≈ 0.031, so 5 of 6 private documents fell out of the top 10, although keyword-only search found them all. RRF assumes absence from a lane means "not relevant"; here it means "not allowed in this lane".
4. **Decision:** OpenAI stays the `present` generation; BGE-M3 is not flipped. If a public-scoped generation is ever served, the fix is to fuse the vector lane only over documents it covers (or run private data keyword + OpenAI), and re-run this table first.

The keyword rows were measured by `tests/integration/evals/test_eval_corpus_pg.py` (real Neon, same harness, fake vectors so no API cost). Replace the pending rows with `evals.run` output.
