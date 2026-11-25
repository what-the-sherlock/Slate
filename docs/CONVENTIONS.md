# Conventions

These rules keep the codebase reliable and easy for humans and Claude to change. When a rule gets in the way, propose changing it. Don't quietly break it.

## Python

- **Version and tooling:** Python 3.12, uv, ruff (lint + format, line length 100), pyright `strict` for `core/` and `basic` elsewhere, import-linter.
- **Types everywhere.** No `Any` in `core/` without a `# reason:` comment. Use `Protocol` for ports, frozen `dataclass` or Pydantic models for domain values, and `NewType` for IDs (`DocumentId`, `SourceId`).
- **Boundaries use Pydantic.** HTTP request/response schemas live in `api/schemas/`. Domain types never import FastAPI. Adapters convert vendor payloads into domain types at the edge.
- **Async I/O** in the API and adapters. CPU-heavy parsing runs in the worker (sync is fine there). Never block the event loop in the API.
- **Small units.** Functions do one thing, around 40 lines at most. Modules have one responsibility. Prefer composition over inheritance; the only class hierarchies are errors and protocols.
- **Naming:** `snake_case` modules and functions, `PascalCase` classes, verbs for functions (`build_chunks`), nouns for values. Name tests `test_<unit>_<behaviour>`.
- **Docstrings** on public functions in `core/`: what, why, and raised errors. Comment *why*, not *what*.
- **No print**. Use `structlog.get_logger(__name__)`. No module-level side effects (no DB connections at import time).
- **Dependencies:** add only with a reason in the PR or commit body. Prefer the standard library and existing deps. Pin versions through `uv.lock`.

### Errors

- `core/domain/errors.py` defines `AppError` → `NotFound`, `PermissionDenied`, `ValidationFailed`, `Conflict`, `RateLimited`, `UpstreamUnavailable`, `BudgetExceeded`, `ConnectorError` (with `retryable: bool`).
- Adapters catch vendor exceptions and re-raise domain errors with the original chained (`raise ... from e`).
- The API maps domain errors to RFC 9457 problem responses in one exception handler. Unexpected errors return 500 with a `request_id`, not internals.
- Never `except Exception: pass`. Broad catches are allowed only at job or request boundaries, where they log with context and record the failure.

### Reliability patterns

- **Timeouts:** every HTTP client has connect and read timeouts. Defaults: 5 s connect, 30 s read; LLM streams use 120 s total.
- **Retries:** use `tenacity` only for idempotent operations, with exponential backoff, jitter, at most 5 attempts, and `Retry-After` honoured. Wrap retries in the adapter, not in services.
- **Rate limits:** each connector declares a limit, enforced by a shared token-bucket helper.
- **Idempotency:** jobs carry `idempotency_key`. Upserts use `ON CONFLICT`. Writes that must be consistent (chunks + embeddings + document version) happen in one transaction.
- **Checkpoints:** long syncs save progress at least every batch, so a killed run resumes.
- **Graceful degradation:** if the reranker or query rewriter fails, continue without it and log a warning. If retrieval fails, return a problem response, never an ungrounded answer.

## Database

- SQLAlchemy 2.0 typed ORM (`Mapped[...]`) for writes; Core `select()` or `text()` for hand-tuned search SQL in `adapters/postgres/search.py`.
- Every query in a service-facing repository takes a `workspace_id`. Repositories never return ORM objects past the adapter; they convert them to domain types.
- Migrations: Alembic, one concern per migration, descriptive slug (`0003_chunk_embeddings_generation`). Include indexes and constraints. Large index builds use `CREATE INDEX CONCURRENTLY` in a non-transactional migration.
- Store all timestamps as UTC `timestamptz` and generate them in the DB (`now()`) unless the source provides one.
- No N+1 queries. Batch inserts with `executemany` or `COPY` for chunks and embeddings.

## API

- REST under `/api/v1`. Nouns for resources, `POST` for actions (`/sources/{id}/sync`).
- Request and response models are explicit Pydantic schemas with examples. Every route has a summary and tags, because the OpenAPI spec is the contract for the frontend client.
- Use cursor pagination (`?cursor=&limit=`, max 100).
- Mutating routes require a session plus a CSRF header, or an API key. Auth and role checks go through dependencies, never inline in handlers.
- Streaming uses SSE with the event contract in `ARCHITECTURE.md` §8.

## TypeScript and Next.js

- `strict: true` and `noUncheckedIndexedAccess`. No `any`. Validate external data with zod.
- Server Components by default. Use Client Components only for interactivity. Data fetching uses TanStack Query with the **generated** client (`pnpm gen:api` from the API's OpenAPI JSON). Never hand-write fetch types.
- Components are small and accessible (labels, keyboard navigation, focus states). Use shadcn/ui primitives. Visual design follows `docs/UI_DESIGN.md`.
- Colocate a component's test (`*.test.tsx`, Vitest + Testing Library). Playwright covers about 5 smoke flows: login, upload, search, chat with citation click-through, sign-up + verify, cross-account isolation.

## Testing

| Layer | Scope | Rules |
| --- | --- | --- |
| Unit | domain, services with fakes, chunker, RRF, citation parser, prompt builder | No network or DB; `pytest-socket` blocks sockets; fast (< 30 s total) |
| Integration | Postgres adapters, migrations, search SQL, ACL filtering, job queue | `@pytest.mark.integration`; runs against a **Neon test branch** (`DATABASE_URL` in CI secrets); each test runs in a transaction rolled back after |
| Contract | OpenAPI snapshot; SSE event schemas; connector fixtures | Breaking changes need a version bump |
| E2E | Playwright smoke flows against a preview deploy | Minimal; not on every commit |
| Evals | Retrieval and answer quality (see `EVALS.md`) | Separate workflow, budget-guarded |

- Fakes for every port live in `tests/fakes/`. The fake LLM and fake embedder are deterministic, so services are testable without keys.
- Property-based tests (Hypothesis) for the chunker and RRF invariants.
- Every bug fix adds a regression test first.
- Target coverage: ≥ 85% for `core/services` and `core/domain`. Coverage serves the tests; it is not their goal.

## Git and CI

- Trunk-based: short-lived branches, squash merge, Conventional Commits.
- CI (`ci.yml`) on every push: ruff, pyright, import-linter, unit tests, web lint, typecheck and tests, an OpenAPI drift check, and `pip-audit`/`pnpm audit`. Integration tests run on `main` and on PRs that label `db`.
- Dependabot weekly for Python, npm and Actions.
- Never force-push `main`. Never commit generated secrets or `.env`.

## Documentation

- Update `ARCHITECTURE.md` when structure or flows change, `DECISIONS.md` for any decision with alternatives, and `ROADMAP.md` status when a milestone completes.
- README sections: what it is, a demo GIF, the architecture diagram, eval results table, how to run, and design decisions (link the ADRs).
