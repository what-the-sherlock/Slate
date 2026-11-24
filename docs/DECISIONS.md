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

## ADR-0016: Direct-to-storage uploads with presigned POST
**Status:** Accepted (2026-11-24)
**Context:** Vercel functions accept at most 4.5 MB per request body, but uploads may be up to 25 MB. Streaming files through the API would also count against Neon's shared 5 GB/month egress twice.
**Decision:** Browsers upload straight to Neon Object Storage with a presigned **POST** whose policy pins the key, the Content-Type and `content-length-range [1, 25 MB]`, so the bucket enforces the limit. The browser computes the file's SHA-256 (WebCrypto) so duplicates are detected before any bytes move. The API returns an HMAC-signed, 15-minute upload token (no server-side state) and, on `complete`, verifies the token, HEADs the object for its size and reads only the first 8 KB to sniff the real type. The worker downloads the file once and rejects it if the SHA-256 doesn't match. Object keys are `uploads/<workspace>/<uploader>/<sha256>.<ext>`, so no member can obtain an upload URL for another member's object. Bucket CORS allows only the app's origin.
**Consequences:** No request-size ceiling from the host. Duplicate uploads cost one small API call. A client that lies about the hash only wastes its own upload, because the worker fails the job. Two system sources ("Uploads", "Public uploads") exist per workspace because documents must share their source's data class (ADR-0012).
**Amendment after the security review (2026-11-24):**
- **The worker is the security gate, not `complete`.** The presigned form stays valid for 10 minutes, so the object can be replaced after `complete` checks it. `process_upload` must download the object once with a byte cap of 25 MB, then hash, sniff, fully validate and parse *those same bytes* (no second GET). It marks the job dead if the SHA-256 differs from the payload's. For DOCX it checks the OOXML structure and caps decompressed size and ratio (zip bombs). For text it decodes the whole file. Tests must cover each of these.
- **Abuse limits:** 30 upload starts per hour and 500 MB of announced bytes per day per uploader, using a generic Postgres rate limiter (`rate_events`, migration 0003). Objects whose upload never completed are swept by the worker (to do: slice 4).
- **Dedup compares access, not just bytes:** a re-upload with a different visibility returns `visibility_differs` with the existing document's id (changing visibility is an explicit action), never "duplicate". The job key is per workspace, so both upload sources share it.
- **Key separation:** upload tokens are signed with `HMAC(SESSION_SECRET, "slate/upload-token/v1")`, never the raw secret. Every future signed artefact derives its own sub-key the same way.
- **The ingestion run is created by the worker when processing starts**, so duplicate `complete` calls can't leave orphan runs. A dead job is retried under the deterministic key `…:retry-of:<dead job id>`.
- **Presigned downloads force `Content-Disposition: attachment`** and a generic content type, so uploaded HTML never renders on the storage origin.
- **Integration tests prove that Neon itself rejects** an oversized body, a changed key, a changed Content-Type and an expired form.

## ADR-0017: Seekworthy-style design system (warm stone, role type scale, panel grammar)
**Status:** Accepted (2026-11-24). Replaces the Answerline/Trakkr direction in `UI_DESIGN.md` §2 and §4.1–4.2.
**Context:** The owner judged the M1 screens visually weak: 14–22 px type and loose spacing made a data tool read like a marketing page, the cool greys looked flat, and every page was a padded column. The owner supplied the Seekworthy codebase (MIT) as the reference for sign-up, dashboard and app chrome.
**Decision:** Adopt Seekworthy's design language, not its code or brand. (1) **Tokens:** warm stone neutrals (Tailwind stone 100–900) with the same blue accent (`#3050F0`). The values change in `web/src/styles/tokens.css` only, so every existing utility follows. (2) **Role type scale** in `globals.css`: `label` 10, `data` 11, `caption` 12 (controls), `body` 13, `ui` 14, `page` 20, `metric` 24, `display` 36, each with a flat 1.5 line height. (3) **Font:** Inter via `next/font/google`, which is self-hosted at build time, replacing the Inter Tight files. (4) **Panel grammar** (`components/panel.tsx`): full-bleed cells split by hairlines, with no floating cards on app pages. (5) **Shell:** a 200 px sidebar that collapses to a 60 px rail, with the state kept in `localStorage`. (6) **Auth:** Seekworthy's `/join` layout (badge, headline, 440 px card, icon-prefixed fields, live password hints, perks) applied to Sign-in and Setup. No open sign-up is added. (7) A **Home** dashboard derived from `GET /sources` only.
**Kept from before, on purpose:** WCAG 2.2 AA. Input borders stay at ≥ 3:1 (`--sl-border-input` `#8F8A85`), where Seekworthy uses a 1.4:1 hairline. Password hints mirror `validate_password` (length only, NIST SP 800-63B) instead of Seekworthy's composition rules. There is still no dark theme.
**Consequences:** No Seekworthy logo, name, copy or assets ship. The 12 px control text is denser; touch inputs stay at 16 px to stop iOS zooming in. Home adds no endpoint and no polling beyond the existing "only while processing" rule, so the Neon budget is unchanged. Search and Chat (M2, M3) must be built with the panel grammar.

## ADR-0018: Client-side connection pool for the API (replaces NullPool)
**Status:** Accepted (2026-11-24). Supersedes the `NullPool` choice in ARCHITECTURE "Two DB URLs".
**Context:** Every API request took 2–5 s on an almost empty database. Measured with `scripts/bench_api.py` (dev laptop → Neon aws-us-east-1): each new connection cost about 1.3 s (TCP + TLS + SCRAM auth is about 6 round trips), and with `NullPool` every unit of work opened one. A signed-in `GET /sources` opened 2 connections and took 4.4 s at p50. Queries themselves were trivial.
**Decision:** The pooled engine keeps a small `QueuePool` (`pool_size=2`, `max_overflow=3`, `pool_timeout=5`, `pool_recycle=120`) on Neon's PgBouncer endpoint, with `prepare_threshold=None` still set. No `pool_pre_ping`, because it adds a round trip to every checkout. Instead, the UoW's first statement (`SET LOCAL statement_timeout`) detects a dead connection (`connection_invalidated`), discards it and retries once, which is safe because no caller work has run yet. The engine times every physical connect into a per-request contextvar (`core/request_metrics.py`), and the access log and the `Server-Timing` header report `db_connects` and `db_connect_ms`.
**Result:** Warm requests open 0 connections. p50 latency: `/me` 2096 → 876 ms, `/auth/status` 2144 → 892 ms, `/sources` 4366 → 1755 ms, `/readyz` 4260 → 1766 ms (n=15).
**Consequences:** Through PgBouncer, held client connections don't pin server connections, because those are lent out one transaction at a time. Recycling at 120 s keeps idle clients well under Neon's 5-minute suspend, which still has to be confirmed in the Neon console (CU budget). On Vercel, a cold instance still pays one connect, but warm (Fluid) instances reuse it. Each request still runs one transaction per adapter call, at about 4 round trips each (BEGIN, SET LOCAL, query, COMMIT). Request-scoped units of work and a role-level `statement_timeout` are the next proposals, not done yet.
**Amendment (2026-11-25): auth hero, fonts, motion and guest access.**
- **Auth hero:** Sign-in and Setup use Seekworthy's onboarding hero: a blue gradient panel with its `noise.png` grain, a white headline, a pill email bar, and an animated "How it works" card. One deliberate deviation: Seekworthy's gradient lightens from the top and drops white small text below AA (2.7–4:1). Ours holds `#3050F0` solid for 560 px (white 5.95:1) and lightens only under the card.
- **Fonts:** Inter and JetBrains Mono are self-hosted with `next/font/local` from Seekworthy's Latin-subset files, replacing `next/font/google` Inter and Geist Mono. The rendering is identical to Seekworthy, and builds need no network for fonts. Latin-ext was dropped because `next/font/local` can't give each file its own `unicode-range`; Latin covers U+0000–00FF.
- **Motion:** small hooks in `web/src/lib/motion.ts` instead of an animation library. Loops pause on hover, when offscreen and when the tab is hidden. Reduced motion shows the finished frame. Entrance helpers live in `lib/reveal.ts` (no `"use client"`), so server components can use them.
- **Guest stays; no open sign-up.** Slate is one workspace: a self-registered member would read every Workspace-visible document and could upload files that spend the ~$5/month OpenAI budget. The guest is a read-only viewer of public sources only (`SECURITY.md`), which gives portfolio visitors a one-click demo. "Sign up" means first-run Setup now and invite links in M7. The guest link is renamed "Explore the live demo".
**Amendment 2 (2026-11-25): one-screen hero, even grain, real logo, illustration story.**
- **One screen:** the auth frame is exactly the viewport tall with `clamp()` spacing; the subtitle and card hide only on very short viewports. Verified by headless screenshots at 1366×768, 1366×900, 1920×1080 and 500×900.
- **Even grain with measured contrast:** gradient `#3050F0 → #4260F1 (55%) → #8396F6`, with white text above the 55% stop only (≥ 5.05:1). Grain is two layers of the same tile; the second fades out top to bottom because overlay blending is ~2.5× weaker on dark blue.
- **Logo:** the owner's mark, hand-traced from the PNG into one SVG path and drawn white on an accent tile; it is also the favicon.
- **Marquee shows integrations Slate doesn't have yet, unlabelled (owner's choice).** A reader shouldn't take the logo wall as a feature list: the connectors that exist are listed in ROADMAP.md and in the Add source dialog, which does label availability.
- **`thinking-orbs` pulled forward from M3 to M1** for the sign-in "Ask" column, wrapped once in `components/thinking-orb.tsx` (UI_DESIGN §5).
- **Setup is a one-question-at-a-time wizard in the pill bar** so it fits the one-screen hero. It's still one form; answered fields stay mounted for autofill, and server field errors jump back to their step.
