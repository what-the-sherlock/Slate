# Security

This is a personal project that indexes **private mail and code**, and it has a public guest login. Treat it like a production system.

## Threat model

| Threat | Mitigation | Test |
| --- | --- | --- |
| Guest or other user sees private content | ACL filter in SQL in `search.py`; the same check on preview, citation and download routes; guest principals = `user:<guest>`, `public` | Seeded private + public fixture; guest search, chat context, citations and previews never include private docs |
| Prompt injection in retrieved text (email, issues, Discord) | Sources go in a delimited data block. The system prompt says to treat them as data. No tools or actions are triggered by model output. Answers can only cite retrieved chunks | Fixture doc containing "ignore previous instructions…"; answer behaviour unchanged |
| Private data sent to free AI tiers that may train on it | ADR-0012: `data_class` on sources, documents and chunks (default `private`); DB CHECKs (public only if ACL has `public`; Gmail always private); `may_send` allows free providers only when **every** input is public, failing closed; BGE-M3 (Workers AI) embeds public chunks only; `usage_ledger.data_class` audits every call | `test_data_class.py` (incl. property test), `test_0001_schema.py` constraint tests |
| Secret leakage | Secrets only in env (Vercel, GitHub Secrets). Connector credentials encrypted with Fernet in `credentials`. structlog processor redacts keys matching `token`, `secret`, `key`, `password`, `authorization` | Log redaction unit test |
| Session theft / CSRF | Session cookie `HttpOnly; Secure; SameSite=Lax`, random 256-bit token stored hashed. CSRF double-submit header on mutating routes. Sessions expire and rotate on login | API tests for missing or invalid CSRF |
| Weak passwords / brute force | Argon2id (argon2-cffi), login rate limit per IP and per account, generic error messages | Rate-limit test |
| Malicious uploads | Max size (25 MB, configurable); MIME sniffed from bytes, not the extension; allowlist (pdf, docx, md, txt, html, csv, mbox); archives not expanded; parsing only in the worker with a time limit per file; no execution | Parser tests with oversized and mislabelled files |
| SSRF via URLs in config or content | Connectors call only fixed vendor hosts. No user-supplied URL fetching in Phase 1 | — |
| Webhook spoofing | Verify signatures (GitHub HMAC SHA-256 and Discord Ed25519 interaction signatures if used) with constant-time compare. Reject stale timestamps | Signature tests |
| Cost abuse (guest spamming chat) | Per-user and per-IP rate limits. Guest uses only free providers on public data. `BudgetGuard` hard stop | Budget guard unit test |
| Dependency vulnerabilities | Dependabot, `pip-audit`, `pnpm audit` in CI, lockfiles committed | CI |

## Rules

- The repo is **public** (for free Actions minutes). Assume everything committed is world-readable: no real emails, tokens or private repo content in fixtures. Generate synthetic fixtures.
- The worker logs must not print document content, because Actions logs on public repos are public. Log IDs and counts only.
- The guest account is read-only. Its role is `viewer`, it has no upload or source management, and it is excluded from `workspace:<id>`.
- Gmail data: import only from a Takeout export that you upload yourself. Keep raw `.mbox` files private in Supabase Storage (private bucket, signed URLs, short TTL). Never commit them.
- Deleting a source deletes its documents, chunks, embeddings and blobs (hard delete after a 7-day soft-delete window).
- Security-relevant changes (auth, ACL, retrieval SQL, prompt templates, upload handling) must be reviewed by the `code-reviewer` subagent before commit.
- ACLs and data classes are enforced across rows by the database (ADR-0012): child rows inherit `workspace_id` and `data_class` through composite foreign keys, and `chunks.acl` is maintained by trigger. Application code must still write ACLs through one service path, but a bug there can't widen access.
- API keys: the auth service must check that the key's user is still a member of the key's workspace on every request (the schema doesn't enforce it).
- `tests/integration/migrations/test_roundtrip.py` drops all tables; it runs only with `SLATE_ALLOW_DESTRUCTIVE_TESTS=1`, which is set only in CI for the dedicated test branch.
