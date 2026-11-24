# Slate

A permission-aware enterprise knowledge assistant, inspired by Onyx and Glean. It ingests documents, GitHub, Discord and Gmail, runs hybrid search over them, and answers questions with verifiable citations. It is a personal portfolio project, so it must be production-quality: reliable, modular, tested and explainable in an interview.

**Current phase:** see `docs/ROADMAP.md` (the "Status" line at the top). Work only on the current milestone unless told otherwise.

## Read before working

| File | Read when |
| --- | --- |
| `docs/ARCHITECTURE.md` | Always. The source of truth for components, data model and flows |
| `docs/CONVENTIONS.md` | Before writing code |
| `docs/SECURITY.md` | Before touching auth, ACLs, retrieval, connectors, prompts, uploads or secrets |
| `docs/EVALS.md` | Before changing chunking, embedding, retrieval, ranking or prompts |
| `docs/DECISIONS.md` | Before changing anything architectural. Add an ADR for every such change |
| `docs/ROADMAP.md` | At the start of each task, to confirm scope |
| `docs/UI_DESIGN.md` | Before building UI beyond a bare skeleton. The owner supplies it. If it is missing, ask |

## Hard constraints (do not violate; ask if a task seems to need it)

- **$0 hosting, no credit card.** Vercel Hobby (web + API), Neon Postgres Free (1 GB), GitHub Actions on a public repo (worker), Neon Object Storage (5 GB, uploads; ADR-0014), Cloudflare Workers AI (free, second embedder). OpenAI is capped at about **$5/month**.
- **No Docker anywhere.** The dev laptop has under 10 GB free. No local Postgres and no local ML models. Develop against Neon branches.
- **Vercel functions have a 300 s maximum.** The API does nothing long-running. Heavy work goes to the worker (GitHub Actions) through the `jobs` table.
- **Neon compute budget is 100 CU-hours a month.** Don't add anything that keeps the database awake: no tight polling, and hourly cron by default.
- **Single workspace in the UI**, but every table and query is scoped by `workspace_id`.
- **Cross-platform tooling.** The developer may be on Windows. Write scripts in Python or as package-manager scripts, never bash-only.

## Stack

- **Backend:** Python 3.12, FastAPI, Pydantic v2, pydantic-settings, SQLAlchemy 2 (typed, async), Alembic, psycopg 3, pgvector, structlog, httpx, tenacity, tiktoken. Managed with **uv**.
- **Frontend:** Next.js (App Router), TypeScript strict, Tailwind, shadcn/ui, TanStack Query, zod, with an API client generated from FastAPI's OpenAPI spec. Managed with **pnpm**.
- **Quality:** ruff (lint + format), pyright (strict on `core/`), import-linter (layer rules), pytest, ESLint, Vitest, Playwright (a few e2e smoke tests).
- **Models:** OpenAI `text-embedding-3-small` at 1024 dimensions (primary embedder) and an OpenAI small chat model (answers). BGE-M3 on Workers AI is the second embedder, used for evals. Gemini and Groq free tiers are used only for query rewriting on public data. Every model sits behind a port.

## Repository layout

```text
web/              Next.js app (Vercel project "web"); proxies /api/* to the API
api/              FastAPI entry point + HTTP layer only (Vercel project "api")
core/             Python package with all business logic (see layering below)
  domain/         pure types and rules, no I/O
  ports/          Protocols: Embedder, LLM, BlobStore, DocumentStore, SearchIndex, JobQueue, Connector...
  adapters/       implementations: postgres/, openai/, workersai/, s3/, connectors/...
  services/       use cases: ingest, search, answer, sources, auth
  config.py       the only place that reads environment variables
worker/           CLI run by GitHub Actions: `uv run python -m worker run`
evals/            questions, runner, stored results
migrations/       Alembic
tests/            unit/ integration/ contract/ (mirrors core/ paths)
docs/             design docs (see table above)
```

**Layering** (enforced by import-linter in CI): `api`, `worker` → `services` → `ports` ← `adapters`; `domain` imports nothing from the project. Services depend on ports, never on adapters. Wiring happens in one composition root per entry point (`api/deps.py`, `worker/main.py`).

## Commands

Keep this section current when tooling changes.

```text
uv sync                                   install Python deps
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run lint-imports                       layer rules
uv run pytest -m "not integration"        fast tests, no network
uv run pytest -m integration              needs DATABASE_URL pointing at a Neon *test* branch
                                          (SLATE_ALLOW_DESTRUCTIVE_TESTS=1 also runs the migration round-trip)
uv run alembic upgrade head               migrations (direct, non-pooled URL)
uv run python -m api.dev --reload         API at :8000 (Windows-safe; = uvicorn api.main:app)
uv run python scripts/export_openapi.py   regenerate openapi.json after API changes
uv run python -m worker run               drain runnable jobs locally, then exit (--once: one job)
                                          (processing uploads calls OpenAI: only when you mean to spend)
uv run python -m evals.run --embedder openai
pnpm -C web install | dev | lint | typecheck | test | gen:api
```

## Non-negotiable engineering rules

1. **Permissions are enforced in SQL, in one place.** All retrieval goes through `core/adapters/postgres/search.py`, which filters `workspace_id` and `acl && :principals` inside the query. Never post-filter in Python. Never rely on the prompt for access control. Previews and citation links re-check access.
2. **Connectors only fetch.** A connector yields `SourceDocument`s and checkpoints. It never parses, chunks, embeds or writes to the DB.
3. **Ingestion is idempotent.** Keys are `(source_id, external_id)` with `checksum`. Re-running a job must not duplicate anything. Every job has an idempotency key.
4. **Every external call** has an explicit timeout. Idempotent calls get retries with exponential backoff and jitter, and honour `Retry-After`. Failures become typed errors (`core/domain/errors.py`), not bare exceptions.
5. **Config comes only from `core/config.py`** (pydantic-settings). Secrets come only from env vars. Never log secrets, tokens or document contents.
6. **Vendor SDKs live only in adapters.** No `import openai` outside `core/adapters/openai/`.
7. **Migrations are Alembic-only and forward-only.** Never edit an applied migration. Every schema change ships with a migration and a test.
8. **Retrieved content is untrusted data.** Keep it out of system instructions. It can never trigger tools or actions.
9. **Citations are validated.** Every `[n]` in an answer maps to a retrieved chunk the user may see; invalid markers are dropped.
10. **No silent scope growth.** Don't add features, dependencies or services that the current milestone doesn't need. Propose them instead.

## Teaching mode (the owner is learning, not just observing)

The owner wants to understand and be able to explain every part of this system in an interview. While working:

- **Before each slice or non-trivial step**, explain in a few sentences *what* you're about to build and *why*: the concept, the problem it solves, and the alternative you didn't pick.
- **After each slice**, give a short "What you just learned" recap: the key concepts (for example connection pooling, `SKIP LOCKED`, ports and adapters), where they live in the code (file links), and one or two questions an interviewer might ask, with a model answer.
- **Name the concept** behind a pattern the first time it appears, and link the relevant file and line.
- **Suggest small hands-on exercises** where useful ("run `uv run pytest -k jobs -v` and break the idempotency check to see the test fail"), but never block progress waiting for them.
- Prefer plain language. Define jargon the first time. Don't pad: the teaching should be dense and useful, not long.

## How to work

- For anything bigger than a small fix, **plan first**: list files, interfaces, migrations, tests and risks. Wait for approval on architectural changes.
- Build in **thin vertical slices**. Each slice compiles, passes all checks and is committed (Conventional Commits, e.g. `feat(ingest): checksum dedup`).
- **Write tests with the code.** Use the fakes in `tests/fakes/` for ports. Unit tests never touch the network.
- Before saying a task is done, run lint, typecheck, import rules and tests, and report the results honestly. If something can't be run (for example no DB URL), say so.
- When you change behaviour or structure, update the matching doc in `docs/`. Add an ADR for any decision a future reader would question.
- If requirements are ambiguous or conflict with these docs, **ask**. Don't guess on schema, security or cost.

## Definition of done

The code is typed and lint-clean, and the import rules pass. Tests cover the new behaviour, including failure paths. Migrations are included and reversible where practical. Docs and ADRs are updated. Nothing violates the hard constraints. Retrieval changes have an eval run recorded (see `docs/EVALS.md`).

## Never

- Commit secrets, `.env` files, OAuth tokens or real email content (test fixtures are synthetic).
- Add Docker, Redis, Celery, OpenSearch or a separate vector DB without an approved ADR.
- Call paid APIs in tests or CI (except the explicit, budget-guarded eval workflow).
- Disable a failing test or type check to make CI green.
- Weaken ACL filtering, CSRF, or input validation "temporarily".
