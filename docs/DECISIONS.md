# Architecture Decision Records

Format: number, title, status, context, decision, consequences. Add new ADRs at the end. Never rewrite an accepted ADR; supersede it.

---

## ADR-0001: Modular monolith with ports and adapters
**Status:** Accepted (2026-11-24)
**Context:** One developer, three weeks, free hosting, components that must stay swappable (embedders, LLMs, index, worker host).
**Decision:** One Python package (`core/`) layered as domain → ports → services, with adapters at the edge and thin entry points (`api/`, `worker/`, `evals/`). Layers are enforced by import-linter.
**Consequences:** Swapping a vendor touches one adapter and one composition root. There is a little ceremony (protocols, fakes) up front.

## ADR-0002: PostgreSQL is the only datastore
**Status:** Accepted
**Context:** Onyx uses Postgres + OpenSearch (+ Redis). We have no budget and a 1 GB Neon free tier.
**Decision:** Postgres holds relational data, ACLs, the job queue, vectors (pgvector, `halfvec`, HNSW) and keyword search (built-in FTS with two configs). Retrieval sits behind a `SearchIndex` port.
**Consequences:** ACLs and content update in one transaction; there are no sync jobs between stores. `ts_rank` is not true BM25; revisit with `pg_search` or OpenSearch when evals justify it.

## ADR-0003: Free, no-card hosting: Vercel + Neon + GitHub Actions
**Status:** Accepted
**Context:** No credit card and no Docker (laptop under 10 GB free). Oracle Always Free needs a card. Render's free web services sleep.
**Decision:** Vercel Hobby hosts `web` (Next.js) and `api` (FastAPI zero-config functions). Neon Free hosts Postgres. GitHub Actions on a public repo is the worker. Supabase Storage holds uploads.
**Consequences:** 300 s function limit, so all heavy work goes to the worker. Hobby is non-commercial only. The repo must stay public (secrets in GitHub Secrets only). Cron frequency is limited by Neon's 100 CU-h/month.

## ADR-0004: Embeddings: OpenAI text-embedding-3-small (1024-d) primary, BGE-M3 secondary
**Status:** Accepted
**Context:** No local models. A budget of about $5/month. We want a measurable comparison for the portfolio.
**Decision:** `text-embedding-3-small` with `dimensions=1024` is the `present` generation. BGE-M3 on Cloudflare Workers AI (free 10k neurons/day) is a second generation for evals. Both use `halfvec(1024)` in `chunk_embeddings`.
**Consequences:** A full backfill costs about $1 per 50M tokens. Switching models is a background reindex plus a generation flip.

## ADR-0005: Answer generation and data-class routing
**Status:** Accepted
**Decision:** OpenAI's small chat model generates answers. Gemini and Groq free tiers are used only for query rewriting, and only when every chunk and message in context is `public`. A `BudgetGuard` enforces the monthly cap.
**Consequences:** Private mail never reaches providers that may train on free-tier data.

## ADR-0006: ACL strings denormalised onto chunks
**Status:** Accepted
**Context:** Onyx stores ACL entries on every chunk and filters in the index.
**Decision:** Principals are `user:`, `group:`, `workspace:`, `public`. `documents.acl` and `chunks.acl` are `text[]` with a GIN index. User principals are resolved at query time.
**Consequences:** One SQL filter enforces access. Visibility changes rewrite a column without re-embedding.

## ADR-0007: Postgres-backed job queue, triggered via repository_dispatch
**Status:** Accepted
**Decision:** A `jobs` table claimed with `FOR UPDATE SKIP LOCKED`, with idempotency keys, heartbeats and a dead state. The API triggers the Actions worker with `repository_dispatch`, plus an hourly cron.
**Consequences:** No Redis or Celery. Moving the worker to a VM later means running the same CLI as a daemon.

## ADR-0008: No cross-encoder by default
**Status:** Accepted
**Context:** Onyx removed its cross-encoder in favour of LLM selection. A CPU reranker is not available to us anyway.
**Decision:** Phase 1 uses RRF only. A `Reranker` port exists. Phase 2 compares LLM selection with hosted rerankers (for example `bge-reranker-base` on Workers AI) on the eval set.

## ADR-0009: Gmail via Takeout import first
**Status:** Accepted
**Context:** Unverified Google OAuth apps in Testing get refresh tokens that expire after 7 days; restricted Gmail scopes need verification.
**Decision:** Ingest a user-uploaded Takeout `.mbox`. A live Gmail API connector waits for a later phase.

## ADR-0010: Same-origin API through a Next.js rewrite
**Status:** Proposed (confirm in M0 spike)
**Decision:** `web` rewrites `/api/*` to the `api` deployment, so cookies are first-party and no CORS is needed.
**Consequences:** If SSE is buffered through the rewrite, supersede this with an API subdomain + CORS + CSRF.

## ADR-0011: Neon pgvector capabilities (M0 spike)
**Status:** Accepted (2026-11-24)
**Context:** ARCHITECTURE §4 and §8 rely on `halfvec`, HNSW with `halfvec_cosine_ops`, and iterative index scans so that ACL-filtered vector search doesn't lose recall. We measured this on the Neon `test` branch with `scripts/spike_pgvector.py` (TEMP tables, nothing persisted).
**Findings:**
- Neon runs Postgres 18.7 with pgvector 0.8.7: `halfvec(1024)`, HNSW `halfvec_cosine_ops` and `hnsw.iterative_scan` are all available.
- Forcing the HNSW path with a selective ACL filter (5k rows, 2% visible) returned on average **0.9 of 10** rows (recall@10 0.10). With `hnsw.iterative_scan = relaxed_order` it returned 10 of 10 (recall@10 0.99).
- With 20k rows and 20% visible, iterative scan again filled all 10 slots. Absolute recall was low (0.19) because uniformly random 1024-d vectors have nearly equal distances; this says nothing about real embeddings, whose recall is measured by the M2 eval harness.
- Unforced, the planner chose exact search through the GIN ACL index plus a sort in both cases, even with 4k of 20k rows visible.
- Building HNSW over 20k halfvec(1024) rows took about 26 s on Neon's free compute.
**Decision:** The vector search query sets `SET LOCAL hnsw.iterative_scan = relaxed_order` in its transaction, alongside `hnsw.ef_search` (value tuned by evals in M2). No over-fetch fallback is needed.
**Consequences:** Filtered ANN keeps returning k rows for guests and other narrow principals. `relaxed_order` can return rows slightly out of distance order, so results are re-sorted by distance before fusion (RRF only uses ranks). Small per-user corpora will often be searched exactly via GIN, which is both correct and cheap.

## ADR-0012: Data isolation from free AI providers
**Status:** Accepted (2026-11-24)
**Context:** The owner's rule: private data must never reach AI providers whose free tiers may train on or retain inputs (Gemini, Groq, Cloudflare Workers AI). ADR-0005 stated the rule, but the schema had nowhere to record which data is private.
**Decision:** Defence in depth, so a mistake in one layer is caught by another:
1. `data_class` (`public` | `private`) on `sources`, `documents` and `chunks`, **defaulting to `private`**. Chunks carry it (like `acl`) so the router sees exactly what was retrieved.
2. Database CHECKs: data may be `public` only if its ACL already contains `public` (readable by anyone), and `gmail_mbox` sources are always `private`.
3. One pure routing rule, `core/domain/data_class.may_send`: a free-tier call is allowed only if **every** input (retrieved chunks, earlier turns, the question) is public. Unknown or missing classes count as private (fail closed).
4. `embedding_generations.data_scope`: a generation served by a free provider (BGE-M3 on Workers AI) is `public`-scoped, so private chunks are never sent to it. Private evals use OpenAI embeddings only.
5. `usage_ledger.data_class` records the most sensitive class of every model call, so it's auditable that no private call went to a free provider.
6. **Rows can't disagree with their parents** (added after the security review): composite foreign keys force `documents.(workspace_id, data_class)` to equal its source's and `chunks.(workspace_id, data_class)` to equal its document's, with `ON UPDATE CASCADE`; a trigger makes `chunks.acl` always a copy of `documents.acl`; a trigger rejects a private chunk's embedding under a `public`-scoped generation. So a Gmail document can't be public, a chunk can't move to another workspace, and narrowing a document's ACL narrows its chunks in the same transaction.
**Workspace-scoping exceptions (deliberate):** `embedding_generations` and `eval_runs` are global, because the embedding model is a system-wide choice. `ingestion_errors` and `messages` are reached only through a workspace-scoped parent (`ingestion_runs`, `chat_sessions`).
**Consequences:** Private data goes only to OpenAI's paid API (no training on API inputs by default). Marking data public is a deliberate act that the database double-checks. A guest-facing query rewrite may use a free provider only for fully public conversations. Isolation tests: `tests/unit/core/domain/test_data_class.py`, `tests/integration/migrations/test_0001_schema.py`.

## ADR-0013: Embedding generations: status plus partial unique indexes
**Status:** Accepted (2026-11-24)
**Context:** Exactly one embedding generation may be `present` (served by search) and at most one `future` (being built). Options: an application check (racy), a singleton pointer row (atomic flip, but `future`/`past` still need tracking), or database-enforced uniqueness.
**Decision:** Keep the `status` column and add partial unique indexes `UNIQUE (status) WHERE status = 'present'` and the same for `'future'`. A flip is two statements in one transaction: demote the old `present` to `past`, then promote the `future`.
**Consequences:** The database makes two present generations impossible, with no race. Readers see the switch atomically at commit. History stays in the same table.

## ADR-0014: Uploads in Neon Object Storage instead of Supabase Storage
**Status:** Accepted (2026-11-24). Supersedes the storage part of ADR-0003.
**Context:** The owner asked to keep everything on Neon. Neon Object Storage is S3-compatible, free on the Free plan (5 GB per project, no card, limits block rather than bill) and available in our region (aws-us-east-1). Buckets branch with the database, so a test branch gets its own copy of the files its rows point at.
**Decision:** Store uploaded originals in a private `uploads` bucket declared in `neon.ts` and provisioned with `neon deploy`. The `BlobStore` port gets an S3 adapter (path-style addressing, SigV4) configured from the AWS-standard env vars Neon injects (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_ENDPOINT_URL_S3`, `AWS_REGION`). Downloads go through short-lived presigned URLs. Supabase is dropped from the stack.
**Consequences:**
- One provider, one credential system, one CLI. No Supabase account is needed.
- **Egress risk:** object downloads count toward the Free plan's 5 GB/month public network transfer, which is shared with Postgres. Exceeding it suspends the database until the next billing period. The worker should download each upload once per processing run, and previews serve extracted text from Postgres rather than the original file.
- Storage is 5 GB per project, so the 25 MB per-file upload limit stays.
- `neon deploy` and `neon env pull` overwrite `DATABASE_URL` in `.env` with the linked branch's URL. Integration tests now refuse to run when `DATABASE_URL` and `DATABASE_URL_DIRECT` point at different Neon endpoints.
- The bucket exists on `production`. Branches created *afterwards* inherit it; the pre-existing `test` branch needs `neon deploy` against it before blob integration tests run.

## ADR-0015: Gmail via an Internal Google Workspace OAuth app
**Status:** Accepted (2026-11-24). Supersedes ADR-0009.
**Context:** The owner wants a "Connect Gmail" button like major platforms, not a Takeout upload. ADR-0009's objections apply to *External* OAuth apps: unverified apps in testing mode get refresh tokens that expire after 7 days, and restricted Gmail scopes need Google's security assessment. An app whose user type is **Internal** to a Google Workspace organisation is exempt from verification and the 7-day expiry, for users inside that organisation.
**Decision:** The Gmail connector (M6) uses OAuth 2.0 with an Internal app in the owner's Google Workspace, scope `gmail.readonly`, offline access (refresh token). The refresh token is stored Fernet-encrypted in `credentials`. Sync uses the Gmail API history endpoint for incremental polling. Gmail sources stay `private` (ADR-0012, enforced by a DB CHECK).
**Consequences:** Only accounts in the owner's Workspace can connect, not personal @gmail.com accounts; the UI says so. The connector needs a stable OAuth redirect URL, so local development uses `http://localhost` (allowed by Google) until the app is deployed. The Takeout `.mbox` import may return later as a fallback for non-Workspace accounts. Needs `CREDENTIALS_FERNET_KEY` and the Google OAuth client id and secret in env.
