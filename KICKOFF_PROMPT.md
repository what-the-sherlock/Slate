# Kickoff prompt for Claude Code (Milestone 0)

Paste everything below the line into Claude Code, in plan mode, from the repo root, after copying in `CLAUDE.md`, `docs/` and `.claude/`.

---

You are the lead engineer on **Slate**, a permission-aware knowledge assistant (Onyx/Glean-style) that I'm building as a portfolio project. The design is already agreed. Your job now is to build the foundation (Milestone 0) to production standards.

## 1. Read first, then plan

Read `CLAUDE.md`, then everything in `docs/` (ARCHITECTURE, CONVENTIONS, SECURITY, EVALS, ROADMAP, DECISIONS). These are the source of truth. Where they are silent, choose the simplest option that fits them, and tell me.

Before writing any code, give me a plan for **Milestone 0 only** (`docs/ROADMAP.md`):

1. The repository tree you will create, with one line per file or folder describing its purpose.
2. The tooling config (pyproject sections, ruff, pyright, import-linter contracts, pytest markers, pnpm scripts) and why each choice.
3. The port interfaces you'll define in `core/ports/` (signatures) and their fakes.
4. The first Alembic migration: tables, constraints and indexes, taken from `ARCHITECTURE.md` §4, plus anything you think is missing (flag additions explicitly).
5. The CI workflow jobs, and the Vercel setup for the two projects (`web`, `api`), including the `/api/*` rewrite.
6. The two spikes (SSE through the rewrite; Neon pgvector version, `halfvec` and iterative scan) and how you'll verify them.
7. Everything **I** must do by hand (accounts, secrets, env vars, Neon branches), as a checklist in order.
8. Risks and open questions.

**Stop after the plan and wait for my approval.**

## 2. After approval: build in slices

Work in this order. Commit after each slice (Conventional Commits). Each slice must pass all checks before the next one starts:

1. Repo skeleton, tooling, pre-commit, `.gitignore`, `.env.example`, README stub.
2. `core/config.py` (pydantic-settings, fail fast in prod), structlog setup with secret redaction, domain error hierarchy, ID types.
3. Ports + in-memory fakes + unit tests for the fakes' contracts.
4. Postgres adapter foundation: async engine/session factories (pooled for API with `NullPool` and prepared statements disabled; direct for migrations and worker), unit-of-work helper.
5. Alembic setup + migration `0001` (extensions `vector` and `citext`; full schema; indexes). Integration test: upgrade on an empty Neon test branch, then assert the tables and indexes exist.
6. FastAPI app factory: request-ID middleware, problem+json exception handler, `/healthz`, `/readyz` (DB + migration head), OpenAPI metadata, Vercel entry point.
7. Next.js skeleton (App Router, TS strict, Tailwind, shadcn/ui init), `/api/*` rewrite, `pnpm gen:api` that generates the typed client from the API's OpenAPI JSON, and a status page that calls `/api/v1/healthz` via the generated client. Keep the UI bare; visual design comes later from `docs/UI_DESIGN.md`.
8. Worker CLI skeleton (`python -m worker run --once`), with a no-op job handler and the job-claim query using `SKIP LOCKED`, plus integration tests for claim, heartbeat, retry and dead-lettering.
9. GitHub Actions: `ci.yml` (Python + web checks, OpenAPI drift check, audits), `sync.yml` (`repository_dispatch` + hourly cron + concurrency group, runs the worker), Dependabot config.
10. Spikes: deploy both Vercel projects and verify that SSE streams unbuffered through the rewrite (a test endpoint that emits 5 events 1 s apart). Verify Neon's pgvector features. Record both results as ADRs (update ADR-0010).

## 3. Quality bar

- Follow every rule in `CLAUDE.md` → "Non-negotiable engineering rules". Respect the hard constraints: no Docker, no local DB, no local models, $0 hosting, Vercel 300 s limit, Neon compute budget.
- Strict typing, small modules, ports and adapters, no vendor SDK outside adapters, config only via `core/config.py`.
- Tests ship with every slice. Unit tests use fakes and never touch the network. Integration tests are marked and use the Neon test branch from `DATABASE_URL`.
- Every external call gets explicit timeouts, and idempotent ones get retries with backoff.
- Keep scripts cross-platform; I may be on Windows.
- Don't add dependencies, services or features beyond Milestone 0 without asking.
- After slices 5, 8 and 10, run the `code-reviewer` subagent and fix its must-fix findings.

## 4. When you finish Milestone 0

Report:
- What was built, slice by slice, with commit hashes.
- The exact output of lint, typecheck, import rules and tests (unit + integration).
- Spike results and the ADRs written.
- Anything skipped or not verified, and why.
- What I need to do next (secrets, Vercel or Neon settings).
- Update `docs/ROADMAP.md` (tick items, set Status to "M0 complete; M1 next") and `CLAUDE.md` → Commands if they changed.

Don't start Milestone 1 until I say so.
