# Roadmap

**Status:** M1, M2, accounts (ADR-0021) and **M3 cited chat complete** (2026-11-25; ADR-0022). Next: M4 GitHub.

Update this line and tick items as work completes. Only the current milestone is in scope.

## Milestones (3-week interview demo, full-time)

### M0 — Foundations (days 1–2)
- [x] Monorepo: `web/`, `api/`, `core/`, `worker/`, `evals/`, `migrations/`, `tests/`, `docs/`
- [x] Tooling: uv, ruff, pyright, import-linter contracts, pytest (+ pytest-socket), pnpm, ESLint, Vitest; `.env.example`; `.gitignore`; pre-commit
- [x] `core/config.py`, structured logging, error hierarchy + problem+json handler, request ID middleware
- [x] Ports with in-memory fakes; Postgres session factory (pooled + direct)
- [x] Alembic migration 0001: full schema from `ARCHITECTURE.md` §4 (incl. pgvector, citext) + A1–A4 (ADR-0012, ADR-0013)
- [x] `/healthz`, `/readyz`; Next.js skeleton with `/api/*` rewrite; generated API client
- [x] CI workflow (`ci.yml`), worker workflow (`sync.yml`); Neon test branch for integration tests; Dependabot. First green run needs the owner's push + secrets
- [ ] ~~Vercel projects `web` and `api` deployed; spike: SSE streams through the rewrite~~ **Deferred (owner, 2026-11-24): Vercel setup waits until an MVP exists. The SSE spike must run before M3 streaming work; ADR-0010 stays Proposed until then.**
- [x] Spike: verify Neon's pgvector version supports `halfvec` and iterative scans (ADR-0011: Postgres 18.7, pgvector 0.8.7)
- Done when: both apps run locally, CI is green, migrations apply to the Neon `test` branch, the pgvector spike is recorded

### M1 — Ingest uploads (days 3–5)
- [x] First-run setup (owner + guest, SETUP_TOKEN), login/logout, sessions + CSRF, atomic login throttling (security-reviewed)
- [x] Upload API: direct-to-storage presigned POST, byte sniffing, checksum dedup, queued job (ADR-0016)
- [x] Pipeline: download once → verify SHA-256 + sniff → parse (pypdfium2, python-docx, Markdown, HTML, CSV, text) → chunk (~512 tokens) → embed (OpenAI, 1024-d) → store in one transaction; runs, errors, usage ledger, budget guard
- [x] Worker sweep of abandoned upload objects (older than a day, unreferenced)
- [x] Worker CLI + `sync.yml` (hourly cron, concurrency group); inline processing for uploads up to 1 MB. On-demand dispatch is deferred (owner decision: no GitHub token yet)
- [x] First screens to UI_DESIGN.md: first-run setup, sign-in (+ guest), app shell, Sources table, upload dialog with progress, source detail with run history and errors
- Done when: an uploaded PDF is searchable within a minute; re-upload of the same file is a no-op

### M2 — Hybrid search + evals (days 6–8)
- [x] Vector + keyword retrieval with ACL filter; RRF; per-document cap; search page with previews (ADR-0019)
- [x] BGE-M3 (Workers AI) as second embedding generation: adapter + idempotent backfill of public chunks (ADR-0020); generation 111 built
- [x] Eval harness and the first 50 questions (51); baselines recorded for keyword-only, OpenAI and BGE-M3
- Done when: Recall@10 is recorded for both embedders; the guest-isolation test passes

### Pre-M3 — Open sign-up (ADR-0021)
- [x] Sign-up with a personal workspace per account; `OWNER_EMAIL` + `SETUP_TOKEN` for the owner; guest and setup wizard removed (migration 0004)
- [x] Email verification and password reset by 6-digit code (SMTP); unverified accounts can't spend; 48 h sweep
- [x] Spam protection: Turnstile, honeypot, per-IP and global sign-up limits, canonical email, disposable domains, kill switch
- [x] Spend caps per workspace and globally, per day and month; storage quota per workspace
- [ ] Owner: integration tests + `alembic upgrade head` on the test branch, then prod; Brevo/Gmail SMTP and Turnstile keys in Vercel; security review

### M3 — Cited chat (days 9–11)
- [x] SSE spike through the Vercel rewrite: streams unbuffered (ADR-0010 Accepted)
- [x] Rewrite → retrieve → gate → assemble (neighbours) → stream (SSE v1) → validate citations → persist (ADR-0022)
- [x] Insufficient-evidence path (gate + sentinel); feedback; budget caps and rate limits; chat UI (UI_DESIGN §4.3)
- [x] Integration tests; `evals.run` gate tuning (CHAT_MIN_SIMILARITY 0.20); `evals.answers`: abstention 0.943, then 1.000 on the targeted re-run, citation validity 1.000, no injection followed; security review fixed
- Note: the neighbour-heading fix (ADR-0022) landed after the last full answer run; it's unit-tested, and the next prompt change should re-run `evals.answers`
- Deferred (ADR-0022): free-tier provider router for rewriting public-only chats (OpenAI does all of it for now)
- Done when: every `[n]` opens its passage; abstention cases pass

### M4 — GitHub (days 12–13)
- [ ] Code via git, README/docs, issues and PRs, webhooks, pruning
- Done when: a push is searchable within the next sync

### M5 — Discord (days 14–15)
- [ ] Bot, backfill, incremental with `after=`, thread/window grouping, channel visibility

### M6 — Gmail + demo data (days 16–17)
- [ ] Gmail OAuth connector, Internal Workspace app (ADR-0015; thread = document, always private; owner's org only, others import `.mbox`); "Load sample data" for new workspaces (ADR-0021)

### M7 — Polish (days 18–21)
- [ ] Filters (source, date), sync status, ingestion history; README with diagram and eval table; demo video; buffer

## Later phases (not in scope until the owner says so)

- **Phase 2 (retrieval):** reranker/LLM selection bake-off, multi-query lanes, contextual chunk headers, BM25 via `pg_search`, near-duplicate detection, multi-document summaries
- **Phase 3 (integrations):** Notion, Google Drive/Confluence, live Gmail API, Slack export import, MCP server, OCR
- **Phase 4 (security):** source permission sync, SSO, row-level security, audit UI, PII redaction
- **Phase 5 (scale):** backups and restore drills, paid Postgres, separate embedding service, load tests, SLOs
