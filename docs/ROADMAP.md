# Roadmap

**Status:** M0–M7 complete (2026-11-27). **M8 answer quality code-complete** (2026-11-27, ADR-0031; owner steps below). **M9 integrations framework code-complete** (2026-11-27, ADR-0032). **M10 connected sources code-complete** (2026-11-27, ADR-0033; owner steps below). **Admin console code-complete** (2026-11-28, ADR-0042, owner-requested; owner steps below).

**Chat redesign (2026-11-26, owner-requested, outside the milestone plan):** answer modes (general knowledge and small talk, ADR-0025), regenerate and edit by superseding (ADR-0024, migration 0005), GitHub sync retries failed items, `scripts/diagnose_sources.py`, and the redesigned Chat page (UI_DESIGN §4.3). Pending owner steps: run `alembic upgrade head` on prod, run `evals.answers` (paid) and record it in EVALS.md, and run the diagnosis script against prod for the 0-document repo.

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
- [x] Vercel projects `web` and `api` deployed; SSE spike through the rewrite (deferred to M3, done there: ADR-0010 Accepted)
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
- [x] Owner: integration tests + `alembic upgrade head` on the test branch, then prod; Brevo/Gmail SMTP and Turnstile keys in Vercel; security review

### M3 — Cited chat (days 9–11)
- [x] SSE spike through the Vercel rewrite: streams unbuffered (ADR-0010 Accepted)
- [x] Rewrite → retrieve → gate → assemble (neighbours) → stream (SSE v1) → validate citations → persist (ADR-0022)
- [x] Insufficient-evidence path (gate + sentinel); feedback; budget caps and rate limits; chat UI (UI_DESIGN §4.3)
- [x] Integration tests; `evals.run` gate tuning (CHAT_MIN_SIMILARITY 0.20); `evals.answers`: abstention 0.943, then 1.000 on the targeted re-run, citation validity 1.000, no injection followed; security review fixed
- Note: the neighbour-heading fix (ADR-0022) landed after the last full answer run; it's unit-tested, and the next prompt change should re-run `evals.answers`
- Deferred (ADR-0022): free-tier provider router for rewriting public-only chats (OpenAI does all of it for now)
- Done when: every `[n]` opens its passage; abstention cases pass

### M4 — GitHub (days 12–13)
- [x] GitHub App install with ownership check, repo picker, one source per repo (ADR-0023)
- [x] Docs (tarball) + issues and PRs with comments; checksum skip; pruning; hourly schedule; Sync now inline; disconnect
- [x] Owner: create the GitHub App, set the secrets, try it on a test repo; security review
- Deferred (owner decision): code indexing, webhooks, dispatch token
- Done when: a push is searchable within the next sync

### Pre-M5 — Private by default (ADR-0026)
- [x] Connector-only ACLs (`sources.created_by`), no public user data (triggers + `allows_public`), backfill scoped to the eval workspace, worker payload checks, migration 0006
- [x] Owner: `alembic upgrade head` on the test branch, `uv run pytest -m integration`, then prod
- [x] Audit follow-ups (M7, ADR-0029): owner exempt from the global daily cap + new-account ramp; GitHub access re-checked every sync; error logs checked

### M5 — Discord (days 14–15)
- [x] One Slate bot with install proof, channel picker by the user's own permissions (private channels ok), one source per server (ADR-0027)
- [x] 90-day backfill, incremental `after=` with 48 h re-read, window/thread grouping, pruning, per-channel cursors, hourly schedule, Sync now inline, disconnect (bot leaves when unused)
- [x] Owner: create the Discord application + bot; turn on Message Content intent and "Requires OAuth2 Code Grant"; add the redirect URL; set `DISCORD_*` in Vercel and `DISCORD_BOT_TOKEN` in GitHub Secrets; try it on a test server; security review
- Done when: a message is searchable after the next sync

### M6 — Email (days 16–17)
- [x] Live IMAP mailbox with an app password, Fernet-encrypted (ADR-0028, supersedes ADR-0015): Gmail, Yahoo, iCloud, Fastmail; Inbox + Sent read-only; 90 days / 2,000 conversations, then hourly; one document per conversation; migration 0007
- [x] `.mbox` import (25 MB) into the same thread documents; file deleted after import
- [x] Owner: set `CREDENTIALS_FERNET_KEY` in Vercel (api) and GitHub Secrets; `alembic upgrade head` (0007) on test then prod; `uv run pytest -m integration`; try a Gmail app password
- Dropped (owner): "Load sample data". Deferred to M7: sender/date filters for email questions
- Done when: a question about an email gets a cited answer after the next sync

### M7 — Polish (days 18–21)
- [x] Filters: source, date and sender (From) chips on Search and Chat, applied in the ACL-filtered SQL
- [x] Shared workspaces (ADR-0030): several per account + switcher, invitations bound to an address, roles, leave/remove deletes what that person connected, explicit per-source sharing; migration 0008
- [x] Audit follow-ups (ADR-0029)
- [x] Live sync progress on source pages and a Recent activity history (polls only while running)
- [x] README with diagram and a generated eval table (`scripts/eval_table.py`)
- [x] Owner: demo video; `APP_URL` in Vercel; migrations 0006–0008 on test, `pytest -m integration`, then prod

### M8 — Answer quality (owner-approved 2026-11-27, ADR-0031)
- [x] Realistic evals: 14 connector-shaped documents (email, Discord, GitHub) with dates and senders, 28 cases, oracle plans for the free retrieval eval, a fixed eval clock for answer evals
- [x] Metadata in context: kind, date (user's zone) and sender on every source; today's date in every prompt; browser time zone on `POST /chat`
- [x] Query planner on every turn (queries, intent, period, source kinds, sender), validated, falling back to the question; user filters win; filters dropped if they hide everything
- [x] Recent lane in `search.py` (newest visible documents) fused by weighted RRF; the similarity gate is skipped for recent and summary questions
- [x] Multi-query fusion; 16 passages in 8k tokens with real neighbour expansion and no repeated chunks
- [x] Prompt: partial answers, dated answers for recent questions
- [ ] Owner: `uv run pytest -m integration` on the test branch; `evals.load`, `evals.run --embedder none|openai`, `evals.answers` (paid, ~$0.25); record the rows in EVALS.md; re-tune `CHAT_MIN_SIMILARITY`
- Done when: the connector cases answer with dated citations, and abstention and point coverage are no worse than the 2026-11-25 run

### M9 — Integrations framework (owner-approved 2026-11-27)
- [x] Provider registry (ADR-0032): declarative specs for GitHub, Discord, email, Gmail, Notion, Linear, Google Calendar, Google Drive, Slack and HubSpot (auth kind, endpoints, scopes, PKCE, client auth, token format, account source, indexed or live)
- [x] One OAuth 2.0 flow: signed state bound to user, workspace and provider; PKCE with a derived verifier; one web callback; code exchange and refresh behind the `OAuthProvider` port
- [x] `connections` table (migration 0009): personal, membership-bound, encrypted tokens, status, expiry, last error, refresh lease
- [x] Token refresh: dead grants mark `needs_reconnect` (`ReconnectRequired`, 409); transient failures keep the token
- [x] Integrations page (UI_DESIGN §4.9): All / Connected / Needs sign-in, search, connect, reconnect, disconnect, the Google notice, callback page
- [x] GitHub, Discord and email shown through the registry, linked to their setup (`/sources?add=…`)
- [ ] Owner: `alembic upgrade head` (0009) on the test branch, `uv run pytest -m integration`, then prod; review UI_DESIGN §4.9
- Done when: a provider is added by writing a spec and an adapter, with no new routes or pages (met: every OAuth provider is a spec; switching one on needs only its adapter and `available=True`)

### M10 — Connected sources (owner-approved 2026-11-27, ADR-0033)
- [x] One sync engine (`ConnectedSources`) and the `SourceConnector` port; migration 0010 (`sources.connection_id`, cascade, CHECKs)
- [x] Gmail (OAuth, Gmail API, IMAP's document rules), Notion, Linear, Google Calendar (indexed window), Google Drive (Docs and text), Slack (public channels joined)
- [x] Refuse and revoke sign-ins missing a required scope or Google's offline grant; Gmail-beside-IMAP warning
- [x] Integrations: Manage dialog (per-account status, Sync now, Open source, Reconnect, Disconnect with counts, Add another account); source pages sync and disconnect connected sources
- [x] Planner kinds for the new sources
- [ ] Owner: register the OAuth apps (`docs/INTEGRATIONS_SETUP.md`), set the secrets in Vercel, GitHub Secrets and `.env`; `alembic upgrade head` (0010) on test, `pytest -m integration`, then prod; connect each provider once and ask a question about it
- Later: HubSpot (live), Slack threads and private channels, Drive PDFs/Sheets/Slides, Calendar as a live lookup

### Admin console (owner-requested 2026-11-28, ADR-0042)
- [x] Audit: sign-up, verification, sign-in, reset, Turnstile and rate limits already existed (ADR-0021), so nothing was rebuilt
- [x] `auth_events` (migration 0014), recorded best-effort on sign-up, verification, sign-in (success, failure, throttled), sign-out and reset, with 90-day retention in the worker's housekeeping
- [x] Sign-in rehashes when `needs_rehash` says so; disabling an account deletes its sessions
- [x] `AdminService` and `/api/v1/admin/*`, which answer 404 to everyone except the verified `OWNER_EMAIL`; responses are allow-listed
- [x] `/admin` page with three tabs: Overview (KPIs, sign-up trend, status breakdown), Users (search, filter, sort, disable/enable, delete) and Security (failed sign-ins, events)
- [ ] Owner: run `alembic upgrade head` (0014, 0015, 0016) and `uv run pytest -m integration` on the test branch, then upgrade prod. Sign in as `OWNER_EMAIL` and review UI_DESIGN §4.10

## Later phases (not in scope until the owner says so)

- **Phase 2 (retrieval):** reranker/LLM selection bake-off, multi-query lanes, contextual chunk headers, BM25 via `pg_search`, near-duplicate detection, multi-document summaries
- **Phase 3 (integrations):** Notion, Google Drive/Confluence, live Gmail API, Slack export import, MCP server, OCR
- **Phase 4 (security):** source permission sync, SSO, row-level security, audit UI, PII redaction
- **Phase 5 (scale):** backups and restore drills, paid Postgres, separate embedding service, load tests, SLOs
