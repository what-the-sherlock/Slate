# Roadmap

**Status:** Milestone 0 (Phase 0) — in progress. Waiting on the owner's schema decision (A1–A4) for migration 0001.

Update this line and tick items as work completes. Only the current milestone is in scope.

## Milestones (3-week interview demo, full-time)

### M0 — Foundations (days 1–2)
- [x] Monorepo: `web/`, `api/`, `core/`, `worker/`, `evals/`, `migrations/`, `tests/`, `docs/`
- [x] Tooling: uv, ruff, pyright, import-linter contracts, pytest (+ pytest-socket), pnpm, ESLint, Vitest; `.env.example`; `.gitignore`; pre-commit
- [x] `core/config.py`, structured logging, error hierarchy + problem+json handler, request ID middleware
- [x] Ports with in-memory fakes; Postgres session factory (pooled + direct)
- [ ] Alembic migration 0001: full schema from `ARCHITECTURE.md` §4 (incl. pgvector, citext)
- [x] `/healthz`, `/readyz`; Next.js skeleton with `/api/*` rewrite; generated API client
- [ ] CI workflow; Neon test branch for integration tests; Dependabot (written; first green run needs the push + `DATABASE_URL_TEST*` secrets; `sync.yml` comes with the worker)
- [ ] ~~Vercel projects `web` and `api` deployed; spike: SSE streams through the rewrite~~ **Deferred (owner, 2026-11-24): Vercel setup waits until an MVP exists. The SSE spike must run before M3 streaming work; ADR-0010 stays Proposed until then.**
- [x] Spike: verify Neon's pgvector version supports `halfvec` and iterative scans (ADR-0011: Postgres 18.7, pgvector 0.8.7)
- Done when: both apps run locally, CI is green, migrations apply to the Neon `test` branch, the pgvector spike is recorded

### M1 — Ingest uploads (days 3–5)
- [ ] Owner bootstrap and guest user seed; login/logout; sessions + CSRF
- [ ] Upload API → BlobStore → job → parse → chunk → embed (OpenAI) → store; checksum dedup; ingestion runs and errors
- [ ] Worker CLI + `sync.yml` (dispatch + hourly cron, concurrency group); inline processing for small uploads
- [ ] Sources page showing status and run history (skeleton UI)
- Done when: an uploaded PDF is searchable within a minute; re-upload of the same file is a no-op

### M2 — Hybrid search + evals (days 6–8)
- [ ] Vector + keyword retrieval with ACL filter; RRF; per-document cap; search page with previews
- [ ] BGE-M3 (Workers AI) as second embedding generation
- [ ] Eval harness and the first 50 questions; baseline for both embedders
- Done when: Recall@10 is recorded for both embedders; the guest-isolation test passes

### M3 — Cited chat (days 9–11)
- [ ] Rewrite → retrieve → assemble → stream (SSE) → validate citations → persist
- [ ] Insufficient-evidence path; feedback; budget guard; provider router by data class
- Done when: every `[n]` opens its passage; abstention cases pass

### M4 — GitHub (days 12–13)
- [ ] Code via git, README/docs, issues and PRs, webhooks, pruning
- Done when: a push is searchable within the next sync

### M5 — Discord (days 14–15)
- [ ] Bot, backfill, incremental with `after=`, thread/window grouping, channel visibility

### M6 — Gmail + guest demo (days 16–17)
- [ ] Takeout `.mbox` import (thread = document, private); guest login on the landing page

### M7 — Polish (days 18–21)
- [ ] Filters (source, date), sync status, ingestion history; README with diagram and eval table; demo video; buffer

## Later phases (not in scope until the owner says so)

- **Phase 2 (retrieval):** reranker/LLM selection bake-off, multi-query lanes, contextual chunk headers, BM25 via `pg_search`, near-duplicate detection, multi-document summaries
- **Phase 3 (integrations):** Notion, Google Drive/Confluence, live Gmail API, Slack export import, MCP server, OCR
- **Phase 4 (security):** source permission sync, SSO, row-level security, audit UI, PII redaction
- **Phase 5 (scale):** backups and restore drills, paid Postgres, separate embedding service, load tests, SLOs
