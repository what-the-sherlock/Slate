# Slate improvement plan

Status: proposal, 2026-11-27. Nothing in this plan has been implemented yet.

**How to use this file.** Work through it one item at a time in a Claude Code session, for example: *"Read docs/IMPROVEMENT_PLAN.md and implement P0-1. Follow CLAUDE.md."* Each item is one thin vertical slice: plan, code, tests, docs or ADR, then a commit. The order below is the recommended order.

**Constraints stay binding** (owner decision). The constraints are $0 hosting, no Docker, Postgres as the only datastore, OpenAI at about $5 a month, and the Neon compute budget. Every item below fits inside them. Ideas from Onyx that don't fit are listed in §6 with the reason they were left out.

---

## 1. Verdict

Slate's foundations are stronger than most RAG portfolio projects:

- **Retrieval:** hybrid retrieval with RRF, ACLs enforced inside SQL, iterative HNSW scans and a query planner.
- **Answers:** a similarity gate plus a model sentinel for abstention, and validated citations.
- **Spending:** budget caps backed by a ledger.
- **Engineering:** layered architecture enforced in CI, and 967 + 212 passing tests.

The retrieval and answer core does not need replacing.

The problems are in three places:

1. **Sync honesty and coverage.** A sync can crash and still be recorded as a success with 0 documents. Files Slate doesn't support are dropped without a word. The UI says "continuing in the background" when nothing is continuing. This is why connected sources show 0 documents (§3).
2. **The visitor experience.** Someone who opens the link has to sign up, verify an email, connect an account and wait for a sync before seeing a single answer. Most portfolio visitors will leave first.
3. **Evals that can't fail.** Retrieval Recall@10 is 1.000 on a small, clean corpus. There are no multi-turn, conflicting-information or sync-coverage cases, so regressions that matter go unseen.

The highest-impact changes, in order:

1. Fix and expose sync failures (P0-1 to P0-4).
2. Add a one-click demo workspace (P0-5).
3. Widen what Drive and GitHub index (P1-1, P1-2).
4. Put titles and metadata into keyword search (P1-3).
5. Make the evals harder (P1-8).

## 2. What was checked (evidence)

| Check | Result |
| --- | --- |
| `ruff check`, `ruff format --check` | pass (314 files) |
| `pyright` | 0 errors |
| `lint-imports` | 4 contracts kept |
| `pytest -m "not integration"` | 967 passed |
| `pnpm -C web lint / typecheck / test` | pass; 212 tests in 32 files |
| `scripts/diagnose_sources.py` against the `.env` database (read-only) | See below |
| Read-only counts on the same database | 0 rows in `connections`; 6 workspaces (1 eval); 5 users |
| Integration tests, Neon evals, paid evals | **Not run** (owner choice: free checks only) |
| Production database, GitHub Actions logs, Vercel settings | **Not inspected**: no access from this session |

Diagnosis output, `.env` database:

```text
discord | Eval discord  run full succeeded seen=0 changed=0 · job sync_discord failed "unexpected ValueError"
github  | Eval github   run full succeeded seen=0 changed=0 · job sync_github  failed "unexpected KeyError"
imap    | Eval email    run full failed: "Slate can't use the saved app password any more."
```

That database holds only eval fixtures. The GitHub and Google sources the owner connected aren't in it, so the database behind the deployed site wasn't inspected. The causes in §3 are proven from code and the eval database. Which one hit the owner's production sources still has to be confirmed with P0-3.

## 3. Why connected sources show 0 documents

Each cause below is either **observed** (seen in data or output) or **from code** (read in the source). Any of them alone produces "connected, 0 documents".

1. **A crash is recorded as a successful run** (observed in the eval database; from code). In [core/services/github.py:384-435](../core/services/github.py) and [core/services/discord_sync.py:164-210](../core/services/discord_sync.py), `status` starts as `"succeeded"`, and only known `AppError` subclasses are caught. Any other exception (KeyError, ValueError, an httpx error that wasn't wrapped, a tarball problem) still runs the `finally` block. That block records **succeeded, seen=0** and calls `save_checkpoint(..., synced=True)`, which also moves `last_synced_at`. The job then fails with only `"unexpected KeyError"` stored. The UI shows a healthy, recently synced, empty source. `mail.py` and `connected.py` already handle this correctly with an `except Exception` that records `failed` and re-raises. The two older engines were never brought in line.
2. **An inline crash is reported to the browser as "background"** (from code). [core/services/inline.py:68-73](../core/services/inline.py) turns an unexpected exception into `InlineOutcome.DEFERRED`. The API answers `status: "background"`, and the callback page says *"The first sync continues in the background."* If the worker never runs that job successfully, nothing ever continues.
3. **The background worker may not be running** (unverified). Sync jobs beyond the first 90 seconds, and every hourly sync, need the GitHub Actions `sync.yml` workflow and all its secrets. Any missing secret disables that handler, and its jobs wait forever. On-demand `repository_dispatch` was deferred in M1 and is not wired (no code uses `GITHUB_DISPATCH_TOKEN`).
4. **The inline sync may be killed by Vercel** (unverified). The inline run takes up to 90 s inside a request, and no `vercel.json` sets a function `maxDuration`. If the project's limit is under about 100 s, the function is killed mid-sync. The run then stays `running`, and the job is reclaimed only after a 10-minute heartbeat timeout, and only if the worker runs.
5. **Drive indexes only Google Docs and .txt/.md** (from code: [core/adapters/google/drive.py](../core/adapters/google/drive.py)). A Drive of PDFs, Sheets and Slides syncs to 0 documents with no explanation. Files whose content can't be read are also dropped without a word.
6. **GitHub indexes only Markdown and text docs, README-like files, issues and PRs, never code** (ADR-0023). A code-heavy repo with no issues gives very few documents, or none, and the UI doesn't say why.
7. **Google OAuth app limits** (external; verify in Google Cloud Console). While the consent screen is in *Testing*, only listed test users can sign in, and their refresh tokens expire after 7 days, after which every Google source needs a reconnect. `gmail.readonly` is a restricted scope, so moving to *Production* requires Google's verification. A portfolio visitor who tries "Connect Gmail" will most likely be blocked.

### Confirmed on production (2026-11-27, owner's diagnosis output plus a read-only reproduction)

- **GitHub, root cause found.** The `what-the-sherlock/Wave` source has 0 docs, 3 runs recorded as "succeeded, seen=0", and a job that failed 5 of 5 times with `unexpected TypeError` and is now `dead`. Reproducing the sync with only GitHub GETs, local parsing and no DB writes gives:
  `TypeError: GitHubApp._send() got multiple values for keyword argument 'headers'` at [core/adapters/github/app.py:380](../core/adapters/github/app.py) (`head_commit` → `_get` → `_send`).
  `head_commit` passes `headers=` through `**kw`, and `_get` adds its own `headers=`. This breaks **every GitHub sync for every repo** before any file is read. Unit tests miss it because `GitHubSync` is tested against a fake `GitHubApi`, and no test calls the real `head_commit`. Cause 1 above then hid the crash as a successful empty run. The diagnosis hint *"the repo was read but held nothing"* was also wrong, because the repo was never read.
- **Gmail: never attempted.** The `sync_connected` job has `attempts=0` and was queued at 13:09, and no run exists. So the inline "first sync" after sign-in didn't run, and no worker has claimed the job since. No new hourly GitHub job has been queued since about 10:50 either. Inference: the GitHub Actions worker hasn't run since then, or it runs without the `sync_connected` handler (which needs `OPENAI_API_KEY` among its secrets). P0-3 steps 2 and 3 confirm which. The callback page hides an inline failure because it reports "Connected" whatever happens.

**What the owner's Sources screen shows, and why** (observed in a screenshot, 2026-11-27): both sources say **Queued · 0 documents**, one with *last synced 3 h ago*, under a "Processing 2 sources" banner. The cause is in [api/routers/sources.py:35-44](../api/routers/sources.py) and [core/adapters/postgres/catalog.py:25](../core/adapters/postgres/catalog.py):

- "Queued" is shown whenever the source has a job in `queued`, `running` **or `failed`** status, because a retryable failed job is waiting for its retry.
- That check comes before the last run's real result.

Together with cause 1 this gives exactly the owner's screen, and the eval database shows the same state:

1. The inline sync crashes, but the run is recorded as *succeeded, 0 seen*. That is the "last synced 3 h ago".
2. The job is marked `failed` with `"unexpected …"` (attempt 1 of 5).
3. No worker has retried it since (cause 3), so the source stays "Queued" with no end. The other source never got a successful run, so it shows "—".

The two most likely causes on production are therefore cause 1 (a crash) and cause 3 (no worker). P0-3 step 1 shows the exception type, and step 2 shows whether the worker runs.

**"It can't answer my messages" follows from this** (inference). With 0 indexed documents, every question finds no passages and fails the similarity gate. It then gets either a general-knowledge answer or "not enough evidence". The retrieval code itself shows no defect for message content: email and Discord threads carry subject, sender and date, and the planner has sender and date filters.

---

## 4. Work items

Each item lists: problem → root cause → change → files → schema → tests → done when → cost.

### P0: broken behaviour and the visitor's first minute

#### P0-0 Fix the GitHub `headers` TypeError (do first; it blocks every GitHub source)

**Status: done in code, 2026-11-27.** `_get` now takes explicit `params` and `headers` instead of `**kw`. The regression tests are in `tests/unit/core/adapters/test_github_app.py` (`head_commit` and the headers on every GET) and `tests/unit/core/services/test_github.py` (`test_sync_with_the_real_github_app_indexes_docs_and_issues`: the real adapter under `GitHubSync`). The step that remains is the owner's: deploy, then "Sync now" on Wave.

- **Problem:** every GitHub sync crashes at `head_commit` (§3, "Confirmed on production").
- **Root cause:** `GitHubApp._get(installation_id, url, **kw)` always sends its own `headers=`, so a caller that passes `headers` (here, `Accept: application/vnd.github.sha`) collides with it.
- **Change:** give `_get` an explicit `headers: Mapping[str, str] | None = None` parameter that is merged over `_HEADERS` and the bearer token. Pass `headers` that way in `head_commit`. Check every other `_get` and `_send` caller for the same pattern.
- **Files:** `core/adapters/github/app.py`.
- **Schema:** none.
- **Tests:** adapter tests with `httpx.MockTransport` for `head_commit`, `repo`, `doc_files` and `issues`, each asserting the request's headers. Also one test that runs the real `GitHubApp` under `GitHubSync`, with a mock transport and fake stores, end to end. That test would have caught this.
- **Done when:**
  - The read-only repro reports doc files and issues parsed for `what-the-sherlock/Wave`.
  - After deploying the API and the worker, "Sync now" on the Wave source gives documents greater than 0.
  - The dead job needs no manual cleanup, because "Sync now" queues a new one.
- **Cost:** $0. The real sync then embeds Wave's docs and issues, a few cents at most.

#### P0-1 Sync crashes must be recorded as failures

**Status: done in code, 2026-11-27 (ADR-0034).** `RunBoundary` in `core/services/sync_run.py` now backs GitHub, Discord, IMAP, mbox and the connected engine. Beyond the plan, it also handles cancellation: the inline 120 s `asyncio.timeout` raises `CancelledError`, which `except Exception` misses, so even `mail` and `connected` used to record a timed-out run as succeeded. GitHub and Discord configs are parsed (`GitHubSourceConfig`, `is_snowflake`). Tests: `tests/unit/core/services/test_sync_run.py`, plus a crash test in each engine's test file. The eval-fixture configs are now reported as "settings are incomplete" instead of KeyError and ValueError.

- **Problem:** §3.1. GitHub and Discord record an unexpected crash as a successful, empty run and advance `last_synced_at`.
- **Root cause:** four sync engines each copy the same run-boundary `try/finally`, and two copies missed the catch-all. The duplication is the defect, so the fix is structural.
- **Change:**
  - Extract one run-boundary helper in `core/services/` (e.g. `sync_run.py`). It owns run creation, the status, `save_checkpoint(synced=…)`, `finish_run`, and the honest `failed` + re-raise on an unexpected exception.
  - Move `github.py`, `discord_sync.py`, `mail.py` and `connected.py` onto it, and remove their hand-written copies.
  - Replace `source.config["installation_id"]` and similar raw lookups with a typed config parse. A bad config becomes a `ConnectorError` with a plain message.
  - Log where an unexpected exception was raised (module, function, line) but not its message, which could contain document text (CLAUDE.md rule 5).
- **Files:** `core/services/{github,discord_sync,mail,connected}.py`, a new `core/services/sync_run.py`, `core/domain/github.py` (config type).
- **Schema:** none.
- **Tests:** one parametrised test across all four engines. A connector that raises `KeyError` must give run status `failed` and an unchanged `last_synced_at`, the job must be retried, and no exception message may be stored. Existing tests must keep passing.
- **Done when:** `diagnose_sources.py` on the eval database shows the Eval github and Eval discord runs as `failed` with a reason. Then fix the eval fixtures' missing config (the KeyError and ValueError themselves).
- **Cost:** $0.

#### P0-2 Tell the user when the first sync failed

**Status: done in code, 2026-11-27 (ADR-0034).** Status comes from `core/domain/source_status.py` (pure, tested). "Sync now" answers from the run (`SyncResult`), which also fixed a case the plan missed: a job that *finished* while its run failed used to be reported as `done`. The callback page shows the failure with Try again and Open source. GitHub, Discord, email, Manage, the source page, Home and the chat empty state all use the new fields. Worker liveness is inferred from overdue runnable jobs (no new table).

- **Problem:** §3.2. The UI says "continues in the background" after a crash.
- **Change:**
  - Add `InlineOutcome.FAILED` for unexpected crashes and for retryable failures whose job is now waiting on a worker that hasn't run in more than 2 hours.
  - The sync endpoints return `status: "failed"` with the run's `error_summary`.
  - The integrations callback page and the Manage dialog show *"First sync failed: <reason>"* with *Try again* and *Open source*.
  - The source page shows the latest run's real status. An empty source never displays as healthy.
  - Fix the status mapping in `api/routers/sources.py` `_status`. A source whose only pending job is `failed` shows **"Retrying: <last_error>"**, not "Queued". A job left `queued` or `failed` for longer than 2 hours with no worker activity shows **"Waiting for the background worker"**, a state you can act on. The "Processing N sources" banner counts only jobs that are `running`, or `queued` less than 2 hours ago.
- **Files:** `core/services/inline.py`, `api/routers/integrations.py`, `api/routers/sources.py`, `web/src/app/(app)/integrations/callback/integrations-callback.tsx`, `manage-dialog.tsx`, the source detail page. Regenerate `openapi.json` and the web client.
- **Schema:** none.
- **Tests:** inline crash → `FAILED`; API contract test; Vitest for the callback's failed state.
- **Cost:** $0.

#### P0-3 Diagnose production, then make diagnosis permanent (owner steps + small code change)

**Status: code done, 2026-11-27.** `scripts/diagnose_sources.py` covers every connector, the connection state, skipped counts, stuck runs, a per-kind worker table, and dead or failed jobs before any "repo held nothing" hint. Hint rules: `tests/unit/scripts/test_diagnose_hints.py`. Owner steps 2–4 are still open.

**Owner steps** (in this order; they settle which part of §3 hit your sources):

1. Point `DATABASE_URL_DIRECT` at the **production** database in a shell (not in `.env`) and run `uv run python scripts/diagnose_sources.py`. Paste the output into the Claude Code session.
2. GitHub → repo → Actions → `sync`: are there hourly runs, and are they green? Do the logs show `github_sync_disabled`, `connected_sync_disabled` or `job_crashed` lines? Check that every secret named in `sync.yml` exists.
3. Vercel → project `api` → Settings → Functions: what is the max duration? It must be at least 120 s, or `INLINE_SYNC_BUDGET_S` must drop below it.
4. Google Cloud Console → OAuth consent screen: is it in Testing or Production, and which test users are listed?

**Code change:** extend `scripts/diagnose_sources.py`.

- Accept every connector in `--connector`. Today it lists only `upload`, `github`, `discord` and `gmail_mbox`.
- Report `connections.status` and `last_error` for OAuth sources.
- Flag runs stuck in `running` for over 10 minutes, which means a killed inline sync.
- Flag jobs that are queued with no job of that kind succeeding in the last 2 hours, which means the worker isn't running.
- Never print *"the repo was read but held nothing"* when the latest job failed. Print the job's error instead.
- Keep it read-only.

**Tests:** unit-test the hint rules with fake rows.

#### P0-4 Explain every document Slate didn't index

**Status: done in code, 2026-11-27 (ADR-0034, migration 0011).** Skips are counted in GitHub (code, vendored paths, too large, the file cap, the tarball cap, empty), Drive (now lists every file: unsupported types, too large, unreadable, empty, the cap) and the connected engine (empty documents, page skips). The GitHub preview is the first sync's real skip summary on the connect page, plus a note in the picker, instead of a tree pre-scan (see the ADR's alternatives). Also fixed: migration 0011 allows the `import` run kind, which every mbox import needed. Other connected connectors (Notion, Linear, Slack, Calendar, Gmail) can report `Page.skipped` but don't yet.

- **Problem:** §3.5 and §3.6. "0 documents" with no reason.
- **Change:**
  - Count skips per run, by reason: `unsupported_type`, `too_large`, `empty_text`, `cap_reached`, `excluded_path`.
  - Store them on `ingestion_runs` and show them on the source page and in the Manage dialog, e.g. *"0 indexed · 37 skipped: 30 PDFs (not supported yet), 7 Sheets."*
  - On GitHub connect, preview what will be indexed: *"Slate indexes docs, issues and PRs, not code. This repo has 1 doc file and 0 issues."*
  - Also count `connected.py`'s `if not doc.sections: return True`, which skips documents silently today.
- **Files:** `core/services/{github,connected,discord_sync,mail}.py` (through the P0-1 helper), `core/adapters/google/drive.py`, `core/adapters/github/app.py`, `core/adapters/postgres/sources.py`, the source detail and manage-dialog UI.
- **Schema:** migration `00xx`: `ingestion_runs.docs_skipped int NOT NULL DEFAULT 0` and `skipped jsonb NOT NULL DEFAULT '{}'` (reason → count). Forward-only, with a migration test.
- **Tests:** Drive with one PDF and one Doc → 1 indexed and `{"unsupported_type": 1}`; the GitHub tree filter; the UI rendering.
- **Cost:** $0.

#### P0-5 One-click live demo for visitors

**Status: deferred by the owner, 2026-11-27.** It would reverse ADR-0021 (no guest flows; paid calls need a verified email). Not to be built unless the owner asks again.

- **Problem:** a visitor's first minute is sign-up, an email code, an empty workspace, then an OAuth screen that Google will probably block for them (§3.7). The product's best feature, a cross-source answer with citations, is never seen.
- **Change:**
  - **Entry point:** a **"Try the live demo"** button on the landing page and the sign-in page.
  - **Session:** it creates a short-lived session (e.g. 2 h) as a `viewer` in a dedicated **demo workspace**, seeded from the synthetic company corpus: uploads, GitHub, Discord and email documents. No account, email or OAuth is involved.
  - **Restrictions:** demo sessions can chat and search only. They can't upload or connect, and they can't see other users.
  - **Spend limits:** a separate daily cap for demo answers (e.g. `DEMO_DAILY_BUDGET_USD=0.30`), a per-IP rate limit, and Turnstile on the button (already configured).
  - **Content:** the chat empty state offers 4–6 tested questions that show the product off. At least one cross-source question (e.g. Discord + GitHub issue + email), one "what changed recently" question, and one with no answer, to show abstention.
  - **Clarity:** a banner says it's a demo with synthetic data.
- **Why a new workspace, not the eval workspace:** evals must stay reproducible, and visitors' chat history must never touch them.
- **Security:** touches auth, so read `docs/SECURITY.md` first and **write an ADR**. Demo principals are `user:<demo>`, `workspace:<demo>`. The demo workspace's sources are shared to `workspace:<demo>`, so the existing ACL rule applies unchanged. No `public` principal is needed, and nothing else changes in `search.py`.
- **Files:** `core/services/auth.py` (demo session), `api/routers/auth.py`, a seeding script based on `evals/load.py`, `web/src/app/(auth)/…` (button), the chat empty state, the budget config.
- **Schema:** possibly a `sessions.kind` or `workspaces.is_demo` flag. Decide in the ADR.
- **Tests:** a demo session can't upload, connect or invite; it never sees non-demo workspaces (an isolation test like the outsider tests); the demo budget cap refuses answers once spent.
- **Cost:** about $0.001 per answer, capped daily.

### P1: answer quality and coverage

#### P1-1 Google Drive: index PDFs, Sheets and Slides

- **Change:**
  - Export Sheets as `text/csv`; the CSV parser already exists, and tables become Markdown.
  - Export Slides as `text/plain`.
  - Download PDFs and parse them with the existing pypdfium2 parser.
  - Keep the 2 MB file cap and the 500-file full pass. Unreadable or too-large files are counted by P0-4.
- **Onyx reference:** `connectors/google_drive/doc_conversion.py` maps Doc → text, Sheet → CSV and Slides → PPTX text.
- **Files:** `core/adapters/google/drive.py`, `core/domain/connected.py`.
- **Schema:** none.
- **Tests:** a transport test per MIME type; the cap and skip counting.
- **Cost:** more chunks to embed (~$0.02 per 1M tokens). The budget guard already caps it.

#### P1-2 GitHub: a cheap repository overview

- **Problem:** "What does this repo do?" fails on code-heavy repos.
- **Change:** per repo, index one synthetic **overview document**:
  - description, topics and languages (from the repo API);
  - the top two levels of the file tree;
  - manifest summaries (`package.json` name, description and scripts; `pyproject.toml` `[project]`; Dockerfile base image);
  - latest release notes.

  That's one document per repo, so the cost is negligible. Full code indexing stays out (ADR-0023's cost reason still holds). Record this in an ADR amending ADR-0023.
- **Files:** `core/adapters/github/app.py`, `core/domain/github.py`, `core/services/github.py`.
- **Tests:** domain tests for overview building, with a fixture tree.
- **Cost:** one small document per repo per changed head.

#### P1-3 Make titles and metadata keyword-searchable

- **Problem:** `chunks.tsv_en` is built from `heading_path + content` only. The document title, repo, channel, sender and labels aren't keyword-searchable, so "find the latest PRD" or "the onboarding doc" miss when those words appear only in the title. The embedding text gets the title, so only the vector lane sees it.
- **Onyx reference:** `indexing/chunker.py` `get_metadata_suffix_for_document_index` adds metadata as natural language for embeddings and as bare values for keyword search.
- **Change:**
  - Add `chunks.search_context text`, filled at ingest with the title plus selected metadata values (repo, channel, sender, labels, state).
  - Generate `tsv_en` from `search_context || heading_path || content`, with the title weighted `A` through `setweight`.
  - Use the same values in the embedding prefix, so both lanes see the same context.
- **Schema:** a migration that adds the column, backfills it from `documents`, and recreates the generated `tsv_en`/`tsv_simple` columns and their GIN indexes. The corpus is small, so this is safe on the free tier; run it at a quiet time. Re-embedding is optional: run the evals first.
- **Tests:** a migration round-trip; title-only and metadata-only eval cases (add them in P1-8 first, and record before/after).
- **Cost:** $0, or about $0.01 if the eval corpus is re-embedded.

#### P1-4 A selection step before generation (behind the planned `Reranker` port)

- **Problem:** the model gets up to 16 passages chosen by RRF rank alone. Noisy passages dilute cross-source answers and invite wrong citations.
- **Onyx reference:** `tools/tool_implementations/search/search_tool.py`, step 3. An LLM selects which retrieved sections to read; keeping each LLM decision small is their stated reason it works.
- **Change:**
  - One gpt-4o-mini call scores the top ~30 fused passages (title, date, kind, first 300 characters each) and returns the ids worth reading, as JSON validated with Pydantic.
  - Keep the best ~10, then do the existing neighbour expansion.
  - If the call fails, fall back to the current RRF order.
  - Skip the step for `recent` and `summary` intents.
  - Enable it only if answer evals improve. Free-tier rerankers can't be used, because ADR-0012 limits free providers to public data.
- **Files:** a new `core/ports/reranker.py`, a new `core/adapters/openai/reranker.py`, `core/services/answer.py`, `evals/answers.py`.
- **Schema:** none.
- **Tests:** a fake reranker in `tests/fakes/`; the fallback path; budget-guard coverage.
- **Cost:** about $0.0004 per question.

#### P1-5 Follow-up questions

- **Change:**
  - **Previous-answer lane:** for turns after the first, add the previous answer's cited documents as an extra RRF lane (weight 1.0, under the same ACL rule in `search.py`). "What about the second one?" or "who owns that?" then stays anchored.
  - **Planner check:** confirm the planner rewrites follow-ups into standalone queries (`search_queries(first_turn=…)`).
  - **Eval cases:** add multi-turn cases (P1-8). There are none today.
- **Files:** `core/services/answer.py`, `core/services/search.py`, `core/adapters/postgres/search.py` (one more ACL-filtered lookup by document ids).
- **Tests:** a 2-turn eval case where turn 2 has no keywords of its own.
- **Cost:** $0.

#### P1-6 Date filters skip documents without a source date

- **Problem:** `_filters_sql` filters on `d.source_updated_at`, but the displayed date is `COALESCE(d.source_updated_at, d.created_at)`. Uploads have no `source_updated_at`, so they vanish from "last week" questions.
- **Change:** filter on the same `COALESCE` expression the results show.
- **Files:** [core/adapters/postgres/search.py:70-75](../core/adapters/postgres/search.py).
- **Tests:** an integration test with an upload and a date filter.
- **Cost:** $0.

#### P1-7 Source diversity for cross-source questions

- **Change:** when the plan has no source filter, the assembled context includes the best passage from each connector that had a hit in the top 50, provided that passage reaches at least half the top passage's fused score. This is a light form of MMR (maximal marginal relevance). The answer prompt already labels each source's `kind`. Also tell the model to connect evidence across kinds and to state conflicts: *"the March email says X; the April issue says Y (newer)"*.
- **Files:** `core/services/answer.py` (assembly), prompts.
- **Tests:** cross-source eval cases (P1-8) must cite at least two connectors.
- **Cost:** $0.

#### P1-8 Evals that can fail

The current 84 cases are mostly single-hop, retrieval is saturated, and the corpus is small and clean. Add:

| Set | Cases | Asserts |
| --- | --- | --- |
| Multi-turn follow-ups | 10 | turn 2/3 retrieves the expected docs; no keywords repeated from turn 1 |
| Cross-source | 8 | answer cites ≥2 connectors (e.g. Discord + GitHub issue + email) |
| Conflicting or updated facts | 5 | answer prefers the newer source and mentions the older one |
| Temporal | 5 | "last week", "before the launch", with a fixed clock |
| Title or metadata only | 6 | keyword lane finds them (P1-3 before/after) |
| Distractors | +30 docs | near-duplicate, stale and off-topic documents added to the corpus, so Recall@10 stops being 1.000 |
| Permissions | existing outsider runs | unchanged: 0 leaks, required |
| Sync coverage (contract tests, $0) | per connector | fixture input → expected indexed/skipped counts, crash → `failed` |

**Answer metrics to add** (paid, small samples):

- Faithfulness: an LLM judge checks that each sentence is supported by its cited passage.
- Citation completeness: every factual sentence carries a citation.
- Latency p50/p95 per stage, from existing logs.

**When to run them:**

- Keyword evals ($0) on every retrieval PR.
- OpenAI retrieval evals (about $0.0001) on every chunking, embedding or ranking change.
- Answer evals (about $0.001 per case) on prompt and selection changes.

Record every run in `docs/EVALS.md` as before.

### P2: polish and portfolio impression

#### P2-1 Follow-up suggestions after each answer

Up to three short follow-up chips under a grounded answer. Generate them in the same call by asking for a final `FOLLOWUPS: ["…"]` line, stripped and never streamed, like the existing sentinels. This adds no second call.

- **Files:** prompts, `SentinelFilter`, `Done` event v1 (an optional field), `turn.tsx`.

#### P2-2 Citation hover card

Hovering an inline `[n]` shows a card with:

- source icon and kind;
- title and date;
- author or sender;
- the cited passage (up to 3 lines, matched text highlighted);
- an **Open in GitHub / Gmail / Drive** link that reuses the ACL-checked preview.

The chips and sources panel already exist. This brings the evidence to the sentence.

#### P2-3 Source health at a glance

The Sources table shows, per source:

- a status dot (healthy, syncing, failed, needs reconnect);
- *indexed · skipped · last synced N min ago*;
- a one-line reason when the source isn't healthy.

The home page shows a single banner if any source is unhealthy.

#### P2-4 A landing page that proves the engineering

The how-it-works page gets:

- the live eval table (from `scripts/eval_table.py` output);
- the architecture diagram;
- "permissions enforced in SQL, 0 leaks in N outsider runs";
- the **Try the live demo** call to action (P0-5).

Add a 60–90 s demo video or GIF to the README. Recruiters read the README and the landing page, not the code.

#### P2-5 Answer feedback loop

`feedback` is already stored. Add an owner-only page listing thumbs-down answers with their retrieved passages, and a one-click *"add to eval set"* that writes a case draft to `evals/questions.yaml`.

#### P2-6 Live Slack search (optional; only if Slack is connected)

**Onyx reference:** `context/search/federated/slack_search.py` searches Slack through its API at question time instead of indexing everything. This would save embedding budget on high-volume chat. Skip it unless Slack usage shows the cost; it adds latency and an extra API dependency.

---

## 5. Onyx comparison (only the subsystems that matter here)

| Subsystem | Slate | Onyx | Gap | Recommendation |
| --- | --- | --- | --- | --- |
| Connector framework | Fetch-only connectors; one engine for OAuth sources; GitHub and Discord have older bespoke engines | ~80 connectors, checkpointed runner | Duplicated run boundary (§3.1) | P0-1: one run boundary for every engine |
| Index attempt status | `succeeded/partial/failed` + errors | `success`, `completed_with_errors`, `failed`, `interrupted`, `canceled` | No "interrupted" state, no skip accounting | P0-2/P0-4; treat a killed inline run as interrupted |
| Drive file types | Docs and text only | Docs, Sheets (CSV), Slides, PDFs, Office | Coverage | P1-1 |
| GitHub | Docs, issues and PRs | PRs by default, issues and files optional | Similar; neither indexes code by default | P1-2 overview document |
| Chunk enrichment | Title and heading prefix for embeddings only | Title prefix, metadata suffix for both lanes; optional contextual RAG summaries | Keyword lane misses title and metadata | P1-3; contextual summaries cost too much for the budget |
| Hybrid retrieval | Vector + FTS, weighted RRF, ACL in SQL | Vector + BM25 in Vespa/OpenSearch, RRF over several query kinds | Comparable; Postgres FTS ranking is weaker than BM25 | Keep; `pg_search` only if Neon offers it (ARCHITECTURE §13) |
| Query understanding | One planner call: 1–3 queries, intent, dates, source, sender | LLM tool calls, a semantic rewrite and keyword queries | Comparable for this scale | Keep; add keyword-style queries to the planner if P1-8 shows misses |
| Selection / reranking | None (RRF order) | LLM selects sections, then LLM-decided expansion | Precision on noisy, cross-source sets | P1-4 (selection only; skip per-document expansion calls for cost) |
| Context assembly | Rank order + neighbours ±1, ≤16 passages | Merges adjacent chunks into sections | Similar | Keep |
| Citations | `[n]` validated against retrieved, ACL-visible chunks; invalid ones dropped | Citation processing in the stream | Slate is as strict or stricter | Keep |
| Permissions | Principals on chunks, enforced in SQL; private by default | Access filters on the index, external permission sync | Slate's is simpler and right for personal sources | Keep; no external group sync needed |
| Follow-ups | History into the planner | Full chat history to the agent | No anchoring to earlier citations | P1-5 |
| Workers | GitHub Actions + `jobs` table (`SKIP LOCKED`) | Celery + Redis, several worker types | Slate's fits $0; its weak point is visibility | P0-3 (detect "worker not running") |
| Evals | Retrieval + answer evals, outsider leak check | Eval tooling in `onyx/evals` | Slate's dataset is too easy | P1-8 |

**Where Slate is better than Onyx:** a single datastore, so ACLs and content change in one transaction; private-by-default sharing; cost guarded by a ledger; abstention designed in; and architecture rules enforced by CI.

## 6. Considered and not recommended (under the binding constraints)

- **Vespa or OpenSearch, a separate vector DB, Redis, Celery, model servers:** they break $0 hosting or the no-Docker rule. Postgres FTS plus pgvector is measured as adequate here.
- **Agentic multi-step research (Onyx `deep_research`, tool loops):** several LLM calls per question breaks the $5/month cap and slows answers. One planner call plus the P1-4 selection call captures most of the gain.
- **Contextual RAG summaries per chunk:** one LLM call per chunk at ingest is too expensive for the budget.
- **Full code indexing for GitHub:** ADR-0023's reason still holds; P1-2 covers "what is this repo".
- **Knowledge graph (Onyx `kg/`):** a non-goal in ARCHITECTURE §1, and no measured need for it.
- **New connectors (Confluence, SharePoint, Jira and others):** first make the existing ones reliable and explainable (P0). Each new one needs OAuth app review, and visitors won't use them; the demo (P0-5) shows cross-source answers better.

## 7. Recommended order

1. **P0-0** (a small fix that unblocks GitHub), then the P0-3 owner steps 2–4 (worker, Vercel, Google). Step 1 is done.
2. P0-1 → P0-2 → P0-4 (sync is honest and explained).
3. P0-5 (visitors see the product working within 10 seconds).
4. P1-8 eval additions **before** P1-3, P1-4, P1-5 and P1-7, so each change is measured before and after.
5. P1-1, P1-2, P1-6 (coverage and correctness), then P1-3, P1-5, P1-7, then P1-4 if the evals justify it.
6. P2 items, starting with P2-4 and P2-2 (the most visible to a visitor).
