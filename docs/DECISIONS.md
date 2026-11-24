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
