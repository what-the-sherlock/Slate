# Security

This is a personal project that indexes **private mail and code**, and anyone can sign up at its public URL (ADR-0021). Treat it like a production system.

## Threat model

| Threat | Mitigation | Test |
| --- | --- | --- |
| Another account sees your content | Each account has its own workspace; every query filters `workspace_id` and `acl && :principals` in SQL (`search.py`), and preview, citation and download routes re-check | Cross-workspace HTTP test (`test_search_routes.py`); outsider fixture (`Principals.for_outsider`) never sees private docs in search, chat context, citations or previews; eval runs exit non-zero on any leak |
| Bulk or bot sign-ups | Turnstile on sign-up, resend and reset (fails closed); honeypot; 3 sign-ups per IP per hour and 50 a day; disposable domains refused; one inbox, one account (`email_canonical`); unverified accounts can't spend and are deleted after 48 h; `SIGNUPS_ENABLED` kill switch | `test_auth.py`, `test_auth_routes.py`, `test_auth_rules.py` |
| Guessing email codes | 6 digits, HMAC'd with a server key, single use, 15 min, 5 tries per code (row-locked), 30 checks per IP per hour, bound to account and purpose | `test_auth.py`, `test_auth_pg.py` (parallel guesses) |
| Prompt injection in retrieved text (email, issues, Discord) | Sources go in a delimited data block. The system prompt says to treat them as data. No tools or actions are triggered by model output. Answers can only cite retrieved chunks | Fixture doc containing "ignore previous instructions…"; answer behaviour unchanged |
| Private data sent to free AI tiers that may train on it | ADR-0012: `data_class` on sources, documents and chunks (default `private`); DB CHECKs (public only if ACL has `public`; Gmail always private); `may_send` allows free providers only when **every** input is public, failing closed; BGE-M3 (Workers AI) embeds public chunks only; `usage_ledger.data_class` audits every call | `test_data_class.py` (incl. property test), `test_0001_schema.py` constraint tests |
| Secret leakage | Secrets only in env (Vercel, GitHub Secrets). Connector credentials encrypted with Fernet in `credentials`. structlog processor redacts keys matching `token`, `secret`, `key`, `password`, `authorization` | Log redaction unit test |
| Session theft / CSRF | Session cookie `HttpOnly; Secure; SameSite=Lax`, random 256-bit token stored hashed. CSRF double-submit header on mutating routes. Sessions expire and rotate on login | API tests for missing or invalid CSRF |
| Weak passwords / brute force | Argon2id (argon2-cffi), login rate limit per IP and per account, generic error messages | Rate-limit test |
| Malicious uploads | Max size (25 MB, configurable); MIME sniffed from bytes, not the extension; allowlist (pdf, docx, md, txt, html, csv, mbox); archives not expanded; parsing only in the worker with a time limit per file; no execution | Parser tests with oversized and mislabelled files |
| SSRF via URLs in config or content | Connectors call only fixed vendor hosts. No user-supplied URL fetching in Phase 1 | — |
| Webhook spoofing | Verify signatures (GitHub HMAC SHA-256 and Discord Ed25519 interaction signatures if used) with constant-time compare. Reject stale timestamps | Signature tests |
| Cost abuse (new accounts burning credits) | Only verified accounts spend. Four caps before every paid call: global per month (hard stop) and per day, and per workspace per month and per day (`core/domain/budget.py`). Storage per workspace: 50 documents, 100 MB. Per-user rate limits | `test_budget.py`, ingest/search/upload quota tests |
| Dependency vulnerabilities | Dependabot, `pip-audit`, `pnpm audit` in CI, lockfiles committed | CI |

## Rules

- The repo is **public** (for free Actions minutes). Assume everything committed is world-readable: no real emails, tokens or private repo content in fixtures. Generate synthetic fixtures.
- The worker logs must not print document content, because Actions logs on public repos are public. Log IDs and counts only.
- **Accounts are isolated by workspace** (ADR-0021): a session resolves to exactly one workspace (`memberships_one_per_user`), and no service accepts a workspace id from the client.
- Gmail data: import only from a Takeout export that you upload yourself. Keep raw `.mbox` files private in Neon Object Storage (private bucket, presigned URLs, short TTL). Never commit them.
- Deleting a source deletes its documents, chunks, embeddings and blobs (hard delete after a 7-day soft-delete window).
- Security-relevant changes (auth, ACL, retrieval SQL, prompt templates, upload handling) must be reviewed by the `code-reviewer` subagent before commit.
- ACLs and data classes are enforced across rows by the database (ADR-0012): child rows inherit `workspace_id` and `data_class` through composite foreign keys, and `chunks.acl` is maintained by trigger. Application code must still write ACLs through one service path, but a bug there can't widen access.
- API keys: the auth service must check that the key's user is still a member of the key's workspace on every request (the schema doesn't enforce it).
- `tests/integration/migrations/test_roundtrip.py` drops all tables; it runs only with `SLATE_ALLOW_DESTRUCTIVE_TESTS=1`, which is set only in CI for the dedicated test branch.
- **Accounts (M1, reviewed 2026-11-24):** signing up with `OWNER_EMAIL` requires `SETUP_TOKEN` (refused in prod without one), so nobody can register the owner's address first; platform admin needs that address *verified*. Login throttling is atomic (attempt recorded before the password check under advisory locks) with limits per (email, IP) 5, per IP 20 and per email 100 per 15 minutes, so a stranger can't lock the owner out. An unknown client IP shares one bucket.
- **Client IP behind a proxy:** set `TRUSTED_IP_HEADER` to the header the platform overwrites (`x-vercel-forwarded-for` on Vercel). Never trust `X-Forwarded-For` from the client. Values are validated.
- **CSRF:** mutating routes need `X-CSRF-Token` matching both the CSRF cookie and the session's stored secret. Pre-login POSTs (signup, login, password forgot and reset) need `X-Requested-With: slate` plus a JSON body. Outside dev, cookies use the `__Host-` prefix.
- **Unverified accounts can't change or spend anything:** routes use `MutatingIdentity` (CSRF + verified email). Only logout, verify and resend use `SessionMutatingIdentity` (CSRF only).
- **Sign-up and reset (ADR-0021, needs the security review before commit):** sign-up says when an email already has an account (unavoidable without a mail-first flow; rate-limited). Reset always answers the same way, but only real accounts get mail, so its timing differs slightly. A reset signs out every session. Email bodies hold codes: never logged (the dev-only mailer prints them, and is never wired outside `ENV=dev`).
- **To do when password change or user disable ships:** delete all of that user's sessions (`SessionStore.delete_for_user`; password reset already does), and rehash passwords on login when `needs_rehash` says so.
- **Uploads (ADR-0016):** the type is decided from the bytes (signature for PDF/DOCX; strict UTF-8 with no NULs for text), never from the name or the browser's Content-Type. The size limit is enforced by the bucket's POST policy. Upload tokens are HMAC-signed, expire in 15 minutes and are bound to the uploading user. Only members and above can upload; only admins and the owner can make an upload public.
- **Bucket CORS:** restrict `AllowedOrigins` to the app's origin. Neon's default allows `*`; that's not a hole on its own (every request still needs a signature), but narrow it on every branch. The test branch allows `http://localhost:3000` only.
- **Ingestion bounds (M1, reviewed 2026-11-24):** at most 400,000 tokens per document; cost estimated before the first paid call and the budget re-checked before every batch; PDF ≤ 2,000 pages and ≤ 5M characters; HTML ≤ 100k elements and depth ≤ 200; DOCX checked for zip bombs before decompression; over-long tokens hard-split. Any crash marks the ingestion run failed. Inline processing has a 120 s limit, then falls back to the worker.
- **Shared upload sources:** run history, errors and pending counts are shown to admins only; members see the documents they may read and track their own uploads (`/jobs/{id}`, uploader or admin only).
- **Sign-in redirects:** `?next=` must parse to a same-origin path (`web/src/lib/safe-next.ts`).
- **Search (M2, reviewed 2026-11-25):** all retrieval SQL is in `core/adapters/postgres/search.py`; both lanes and document preview filter workspace, ACL, live document and non-deleting source inside the query. Preview returns the same 404 for missing and forbidden documents. `POST /search` needs CSRF and a verified email (it may spend a paid query embedding) and is limited to 60 per user per minute, 500 characters and 20 source filters. Query text is never logged. Highlighting in the UI renders text nodes only, never HTML.
- **Chat (M3, ADR-0022):** retrieval goes through `search.py` only, so the model sees only passages this user may read (an outsider-fixture test checks the request sent to the model). Sources go in a delimited user-turn block with closing tags escaped; the system prompt never contains retrieved text; model output triggers nothing. Citations are confirmed per n against the shown sources, invalid markers are dropped before storage, and the UI makes badges only from `citation` events. Answers render as Markdown with `skipHtml`, and only `http(s)` and `cite:n` URLs survive. Chats, messages and feedback are reached only through `chat_sessions` filtered by workspace and owner. Chat needs CSRF and a verified email, is limited to 6 per minute and 50 per day per user, and is budget-checked before streaming. Questions and answers are never logged. Injection fixture: `evals/corpus/customers/partner-notes.md` (`evals.answers` flags any answer that follows it).
- **Integrations (M9, ADR-0032):**
  - A connection is personal: rows are scoped by workspace *and* user in every query, belong to a membership (composite FK, `ON DELETE CASCADE`: leaving deletes them), and only their owner can list, use or remove them. Viewers and unverified accounts can't connect.
  - State is HMAC-signed under its own key (`slate/integrations/v1`), lasts 10 minutes and is bound to user, workspace and provider; the callback must come from the same session. PKCE (S256) wherever the provider supports it, with the verifier derived from the state's nonce by that key: never stored, never in a URL.
  - The provider redirects to the web app, which removes `code` and `state` from the address bar and sends them once in a CSRF-protected POST body, so they never reach API logs.
  - Tokens are one Fernet ciphertext per row (`CREDENTIALS_FERNET_KEY`, rotatable). The API never returns them; responses carry the account label, status and the provider's error *code* only.
  - Only the spec's fixed https endpoints are called, with no redirects followed. The code exchange and refreshes are never retried (both are single use); refreshes run under a lease so a rotated refresh token can't be raced.
  - A dead grant marks the connection `needs_reconnect` and stops using it; disconnect revokes at the provider where possible and deletes the tokens.
  - Accepted: Google's restricted scopes (Gmail) run as an unverified app (warning screen, 100 users) until a paid CASA assessment; the connect dialog says so.
- **Query planner (M8, ADR-0031):** the planner sees the question and recent turns as a delimited, escaped data block, and its output is data: validated JSON that can only *narrow* retrieval (queries, dates, connector kinds, sender), never widen it, because ACLs are applied in SQL regardless. The user's own filters always win. Bad output falls back to the question. Calls are classed private in the ledger. Neither queries nor plans are logged, only counts and the intent. Source metadata shown to the model (sender, title, section) is escaped like content, so a crafted author name can't close a tag. Document URIs are not sent to the model.
- **GitHub (M4, ADR-0023):**
  - Read-only app permissions (Contents, Issues, Pull requests, Metadata).
  - An installation is accepted only after GitHub confirms, with the user's own OAuth code, that this user can access it. The user token is revoked at once.
  - The install state (15 min) and the picker grant (30 min) are HMAC-signed and bound to the user and workspace, and they travel in request bodies, never URLs. The callback page removes `code` and `state` from the address bar, and React's double effect can't send the single-use code twice.
  - Installation tokens live in memory only, so no GitHub secret is stored per user.
  - Repo names are validated before any request. Tarballs are capped at 50 MB, and only regular files are read (no symlinks or traversal).
  - Connecting and disconnecting need admin or owner. Sync now needs member and is limited to one per repo per 5 minutes.
  - Every repo, public or private, is workspace-only private data. The grant lists only repos this user can read. Tarballs are streamed with caps on entries and unpacked size. Tokens never leave `api.github.com`: pagination is host-checked and token calls don't follow redirects. Worker logs carry counts and statuses only.
- **Workers AI (BGE-M3) is a free tier (ADR-0012):** only public chunks are ever sent (public-scoped generation, checked in code and by trigger). Eval questions are synthetic.
- **Private by default (ADR-0026, migration 0006):**
  - A connected source is readable only by its connector (`user:<id>`). Sharing is explicit (M7).
  - There is no public user data: triggers allow `public` data or the `public` principal only in workspaces with `allows_public` (the eval corpus), and the BGE-M3 backfill reads only those.
  - `process_upload` refuses a payload whose source, ACL or data class doesn't belong to the job's workspace and uploader.
  - Sync and disconnect 404 for a source the caller can't see.
- **Discord (M5, ADR-0027):**
  - Read-only bot permissions. The install is accepted only when Discord's token response names the same server, which proves Manage Server.
  - The user's token is used once, to read their roles, and revoked. The bot token is in env only.
  - The grant lists only channels the user can read, computed with Discord's permission algorithm; `connect` re-checks the grant and that the bot can read the channel. Private threads are never indexed.
  - State and grant are HMAC-signed under their own key (`slate/discord/v1`), bound to user and workspace, and travel in bodies; the callback strips the code from the URL.
  - Ids are validated as snowflakes before use in URLs; only discord.com is called, with no redirects; the redirect URI must be https outside dev.
  - Message text is untrusted: sections are built directly (no Markdown parsing), and the prompt boundary of ADR-0022 applies.
  - Worker logs carry counts and statuses only.
  - Accepted: access is checked at connect only.
- **Email (M6, ADR-0028):**
  - IMAP only to fixed provider hosts (never user-supplied), over TLS with certificate checks.
  - Read-only by construction: EXAMINE, `BODY.PEEK[]`, and no commands that modify the mailbox.
  - The app password is Fernet-encrypted (`CREDENTIALS_FERNET_KEY`, rotatable), sent once in a CSRF-protected body, never logged, echoed (422s carry field names only) or kept in config, and deleted with its source (trigger + code).
  - 5 login attempts per hour and 20 per day per user.
  - Only Inbox and Sent; attachments are never read; messages over 10 MB are skipped.
  - Email is always private data, readable only by its connector.
  - `.mbox` imports reuse the upload gate (25 MB, sniffed, SHA-256 re-verified by the worker); the job checks that the object belongs to the uploader; the raw file is deleted after import; imports never run inline (the inline runner refuses foreign job kinds).
  - Accepted: deletions in the mailbox aren't propagated yet, and an app password could also send mail, though Slate never does.
- **Audit follow-ups (M7, ADR-0029):**
  - The owner's workspace is exempt from the global daily cap, but not the monthly hard stop.
  - New workspaces get $0.01/day for their first 7 days.
  - GitHub: the user token is kept Fernet-encrypted and re-checked every sync. If access can't be verified, the sync pauses and keeps the documents; if access is confirmed lost, the repo's documents are removed.
  - Unused tokens are swept after 24 h.
- **Shared workspaces (M7, ADR-0030):**
  - A session's active workspace is re-checked against memberships on every request, so a removal takes effect immediately.
  - Invitation tokens are 256-bit, stored hashed, single use, valid 14 days and bound to the invited address. They live in the link's fragment (never in server logs or queries), and accepting takes a click.
  - Only a source's connector can share it; sharing rewrites document ACLs in the same transaction, and every share is audited.
  - Leaving deletes everything that person connected there.
  - The owner can't leave or be removed (enforced in the service and in SQL).
  - The budget exemption never passes to invitees.
