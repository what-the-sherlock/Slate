"""Initial schema (ARCHITECTURE §4) with approved additions A1-A4.

A1 data isolation: data_class on sources, documents, chunks and usage_ledger, with
   CHECKs so data can only be 'public' if its ACL already includes 'public', and
   Gmail can never be public (ADR-0012).
A2 CHECK constraints on every status/kind/role column.
A3 partial unique indexes: at most one 'present' and one 'future' embedding generation.
A4 indexes for the lists the M1-M3 screens and the job queue read.

Isolation is enforced across rows, not just within one (security review, 2026-11-24):
* composite foreign keys force documents.{workspace_id,data_class} = their source's and
  chunks.{workspace_id,data_class} = their document's (ON UPDATE CASCADE), so e.g. a
  Gmail document can never be public and a chunk can't move to another workspace;
* triggers keep chunks.acl a copy of documents.acl (search filters on chunks.acl);
* a trigger stops private chunks being stored under a public-scoped generation.

Revision ID: 0001
Revises:
Create Date: 2026-11-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = r"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS citext;

-- ===================================================================== identity
CREATE TABLE workspaces (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL,
    slug        citext NOT NULL UNIQUE,
    settings    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email          citext NOT NULL UNIQUE,
    name           text NOT NULL,
    password_hash  text,                         -- NULL for the guest (no password login)
    is_guest       boolean NOT NULL DEFAULT false,
    created_at     timestamptz NOT NULL DEFAULT now(),
    disabled_at    timestamptz,
    CONSTRAINT users_login_method CHECK (is_guest OR password_hash IS NOT NULL)
);

CREATE TABLE memberships (
    workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role          text NOT NULL CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, user_id)
);
CREATE INDEX memberships_user_idx ON memberships (user_id);

CREATE TABLE groups (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name          text NOT NULL,
    external_ref  text,                           -- reserved for Phase 4 permission sync
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, name)
);

CREATE TABLE group_members (
    group_id  uuid NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    user_id   uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, user_id)
);
CREATE INDEX group_members_user_idx ON group_members (user_id);

CREATE TABLE sessions (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash    bytea NOT NULL UNIQUE,         -- SHA-256 of the cookie token; raw token never stored
    csrf_secret   text NOT NULL,
    expires_at    timestamptz NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    ip            inet,
    user_agent    text
);
CREATE INDEX sessions_user_idx ON sessions (user_id);
CREATE INDEX sessions_expires_idx ON sessions (expires_at);

CREATE TABLE api_keys (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    prefix        text NOT NULL UNIQUE,
    key_hash      bytea NOT NULL,
    scopes        text[] NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_used_at  timestamptz,
    revoked_at    timestamptz
);

-- ===================================================================== sources
CREATE TABLE credentials (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id      uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    kind              text NOT NULL,
    secret_encrypted  bytea NOT NULL,            -- Fernet ciphertext; key only in env
    created_by        uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sources (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id       uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    connector          text NOT NULL CHECK (connector IN ('upload', 'github', 'discord', 'gmail_mbox')),
    name               text NOT NULL,
    config             jsonb NOT NULL DEFAULT '{}'::jsonb,
    credential_id      uuid REFERENCES credentials(id) ON DELETE SET NULL,
    visibility         text[] NOT NULL CHECK (cardinality(visibility) >= 1),
    data_class         text NOT NULL DEFAULT 'private' CHECK (data_class IN ('public', 'private')),
    schedule_interval  interval NOT NULL DEFAULT interval '1 hour',
    status             text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'deleting')),
    checkpoint         jsonb,
    last_synced_at     timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, name),
    UNIQUE (id, workspace_id),               -- target for workspace-checked FKs
    UNIQUE (id, workspace_id, data_class),   -- target for the documents FK below
    -- A1: only data anyone may already read can be classed public.
    CONSTRAINT sources_public_requires_public_acl
        CHECK (data_class = 'private' OR 'public' = ANY (visibility)),
    -- A1: email is always private.
    CONSTRAINT sources_gmail_always_private
        CHECK (connector <> 'gmail_mbox' OR data_class = 'private')
);
CREATE INDEX sources_workspace_idx ON sources (workspace_id);

CREATE TABLE ingestion_runs (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id   uuid NOT NULL,
    source_id      uuid NOT NULL,
    kind           text NOT NULL CHECK (kind IN ('full', 'incremental', 'prune', 'upload', 'webhook')),
    status         text NOT NULL DEFAULT 'queued'
                   CHECK (status IN ('queued', 'running', 'succeeded', 'partial', 'failed', 'cancelled')),
    started_at     timestamptz,
    finished_at    timestamptz,
    docs_seen      integer NOT NULL DEFAULT 0 CHECK (docs_seen >= 0),
    docs_changed   integer NOT NULL DEFAULT 0 CHECK (docs_changed >= 0),
    docs_failed    integer NOT NULL DEFAULT 0 CHECK (docs_failed >= 0),
    error_summary  text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (source_id, workspace_id) REFERENCES sources (id, workspace_id) ON DELETE CASCADE
);
CREATE INDEX ingestion_runs_source_idx ON ingestion_runs (source_id, created_at DESC);

CREATE TABLE ingestion_errors (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id       uuid NOT NULL REFERENCES ingestion_runs(id) ON DELETE CASCADE,
    external_id  text,
    stage        text NOT NULL CHECK (stage IN ('fetch', 'parse', 'chunk', 'embed', 'store')),
    message      text NOT NULL,
    retryable    boolean NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ingestion_errors_run_idx ON ingestion_errors (run_id);

-- ===================================================================== content
CREATE TABLE documents (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id       uuid NOT NULL,
    source_id          uuid NOT NULL,
    external_id        text NOT NULL,
    uri                text NOT NULL,
    title              text NOT NULL,
    mime               text NOT NULL,
    author             text,
    checksum           text NOT NULL,
    version            integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    source_created_at  timestamptz,
    source_updated_at  timestamptz,
    metadata           jsonb NOT NULL DEFAULT '{}'::jsonb,
    acl                text[] NOT NULL CHECK (cardinality(acl) >= 1),
    data_class         text NOT NULL DEFAULT 'private' CHECK (data_class IN ('public', 'private')),
    deleted_at         timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, external_id),
    UNIQUE (id, workspace_id, data_class),   -- target for the chunks FK below
    CONSTRAINT documents_public_requires_public_acl
        CHECK (data_class = 'private' OR 'public' = ANY (acl)),
    -- Same workspace and data class as the source; follows source changes.
    CONSTRAINT documents_match_source
        FOREIGN KEY (source_id, workspace_id, data_class)
        REFERENCES sources (id, workspace_id, data_class) ON UPDATE CASCADE ON DELETE CASCADE
);
CREATE INDEX documents_workspace_source_idx ON documents (workspace_id, source_id);
CREATE INDEX documents_deleted_idx ON documents (deleted_at) WHERE deleted_at IS NOT NULL;

CREATE TABLE chunks (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id   uuid NOT NULL,
    workspace_id  uuid NOT NULL,
    ordinal       integer NOT NULL CHECK (ordinal >= 0),
    heading_path  text,
    content       text NOT NULL,
    token_count   integer NOT NULL CHECK (token_count >= 0),
    acl           text[] NOT NULL CHECK (cardinality(acl) >= 1),
    data_class    text NOT NULL DEFAULT 'private' CHECK (data_class IN ('public', 'private')),
    metadata      jsonb NOT NULL DEFAULT '{}'::jsonb,
    tsv_en        tsvector GENERATED ALWAYS AS
                  (to_tsvector('english', coalesce(heading_path, '') || ' ' || content)) STORED,
    tsv_simple    tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
    UNIQUE (document_id, ordinal),
    CONSTRAINT chunks_public_requires_public_acl
        CHECK (data_class = 'private' OR 'public' = ANY (acl)),
    -- Same workspace and data class as the document; follows document changes.
    CONSTRAINT chunks_match_document
        FOREIGN KEY (document_id, workspace_id, data_class)
        REFERENCES documents (id, workspace_id, data_class) ON UPDATE CASCADE ON DELETE CASCADE
);

-- chunks.acl is always a copy of its document's acl (search filters on chunks.acl).
CREATE FUNCTION chunks_copy_document_acl() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    SELECT d.acl INTO NEW.acl FROM documents d WHERE d.id = NEW.document_id;
    RETURN NEW;
END $$;
CREATE TRIGGER chunks_acl_from_document
    BEFORE INSERT OR UPDATE OF acl, document_id ON chunks
    FOR EACH ROW EXECUTE FUNCTION chunks_copy_document_acl();

CREATE FUNCTION documents_propagate_acl() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE chunks SET acl = NEW.acl WHERE document_id = NEW.id;
    RETURN NULL;
END $$;
CREATE TRIGGER documents_acl_to_chunks
    AFTER UPDATE OF acl ON documents
    FOR EACH ROW WHEN (OLD.acl IS DISTINCT FROM NEW.acl)
    EXECUTE FUNCTION documents_propagate_acl();
CREATE INDEX chunks_acl_gin ON chunks USING gin (acl);
CREATE INDEX chunks_tsv_en_gin ON chunks USING gin (tsv_en);
CREATE INDEX chunks_tsv_simple_gin ON chunks USING gin (tsv_simple);
CREATE INDEX chunks_workspace_document_idx ON chunks (workspace_id, document_id);

CREATE TABLE embedding_generations (
    id          smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    model       text NOT NULL,
    dims        integer NOT NULL CHECK (dims = 1024),  -- must match chunk_embeddings.embedding
    status      text NOT NULL CHECK (status IN ('future', 'present', 'past')),
    -- Embedding providers allowed to see this data class (ADR-0012): a free provider's
    -- generation is 'public' only, so private chunks are never sent to it.
    data_scope  text NOT NULL DEFAULT 'all' CHECK (data_scope IN ('all', 'public')),
    created_at  timestamptz NOT NULL DEFAULT now()
);
-- A3: at most one present and one future generation, enforced by the database.
CREATE UNIQUE INDEX embedding_generations_one_present
    ON embedding_generations (status) WHERE status = 'present';
CREATE UNIQUE INDEX embedding_generations_one_future
    ON embedding_generations (status) WHERE status = 'future';

CREATE TABLE chunk_embeddings (
    chunk_id       bigint NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    generation_id  smallint NOT NULL REFERENCES embedding_generations(id) ON DELETE CASCADE,
    embedding      halfvec(1024) NOT NULL,
    PRIMARY KEY (chunk_id, generation_id)
);
-- Per-generation HNSW indexes are created when a generation is registered (CONCURRENTLY).

-- Never store a private chunk under a generation served by a free provider (ADR-0012).
CREATE FUNCTION chunk_embeddings_respect_scope() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM embedding_generations g, chunks c
        WHERE g.id = NEW.generation_id AND c.id = NEW.chunk_id
          AND g.data_scope = 'public' AND c.data_class <> 'public'
    ) THEN
        RAISE EXCEPTION 'private chunk % in public-scoped generation %', NEW.chunk_id, NEW.generation_id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'chunk_embeddings_scope';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER chunk_embeddings_scope
    BEFORE INSERT OR UPDATE ON chunk_embeddings
    FOR EACH ROW EXECUTE FUNCTION chunk_embeddings_respect_scope();

-- ===================================================================== conversation
CREATE TABLE chat_sessions (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title         text,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX chat_sessions_user_idx ON chat_sessions (workspace_id, user_id, created_at DESC);

CREATE TABLE messages (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  uuid NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    parent_id   uuid REFERENCES messages(id) ON DELETE SET NULL,
    role        text NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content     text NOT NULL,
    model       text,
    usage       jsonb,
    latency_ms  integer CHECK (latency_ms >= 0),
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX messages_session_idx ON messages (session_id, created_at);

CREATE TABLE message_citations (
    message_id   uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    n            smallint NOT NULL CHECK (n >= 1),
    -- Chunks are replaced on re-ingest; the citation survives and points at the document.
    chunk_id     bigint REFERENCES chunks(id) ON DELETE SET NULL,
    document_id  uuid REFERENCES documents(id) ON DELETE SET NULL,
    PRIMARY KEY (message_id, n)
);

CREATE TABLE feedback (
    message_id  uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    rating      smallint NOT NULL CHECK (rating IN (-1, 1)),
    reason      text,
    comment     text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (message_id, user_id)
);

-- ===================================================================== operations
CREATE TABLE jobs (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id     uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    kind             text NOT NULL,
    payload          jsonb NOT NULL DEFAULT '{}'::jsonb,
    status           text NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'dead')),
    attempts         integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts     integer NOT NULL DEFAULT 5 CHECK (max_attempts >= 1),
    run_after        timestamptz NOT NULL DEFAULT now(),
    locked_by        text,
    locked_at        timestamptz,
    heartbeat_at     timestamptz,
    last_error       text,
    idempotency_key  text NOT NULL UNIQUE,
    created_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz,
    CONSTRAINT jobs_running_is_locked CHECK (status <> 'running' OR locked_by IS NOT NULL)
);
-- A4: the claim query scans only runnable jobs, oldest due first.
CREATE INDEX jobs_runnable_idx ON jobs (run_after, created_at, id) WHERE status IN ('queued', 'failed');
-- A4: stale-heartbeat reclaim scans only running jobs.
CREATE INDEX jobs_running_heartbeat_idx ON jobs (heartbeat_at) WHERE status = 'running';

CREATE TABLE audit_log (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    actor_id     uuid REFERENCES users(id) ON DELETE SET NULL,
    action       text NOT NULL,
    target_type  text,
    target_id    text,
    data         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_workspace_idx ON audit_log (workspace_id, created_at DESC);

CREATE TABLE usage_ledger (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    day         date NOT NULL DEFAULT current_date,
    provider    text NOT NULL,
    model       text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('embed', 'chat', 'rewrite', 'judge')),
    -- A1 audit: the most sensitive data class in the call's input.
    data_class  text NOT NULL CHECK (data_class IN ('public', 'private')),
    tokens_in   integer NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
    tokens_out  integer NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
    cost_usd    numeric(12, 6) NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX usage_ledger_day_idx ON usage_ledger (day);

CREATE TABLE eval_cases (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id           uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    question               text NOT NULL,
    expected_document_ids  uuid[] NOT NULL DEFAULT '{}',
    tags                   text[] NOT NULL DEFAULT '{}',
    notes                  text
);

CREATE TABLE eval_runs (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    git_sha        text NOT NULL,
    generation_id  smallint REFERENCES embedding_generations(id) ON DELETE SET NULL,
    config         jsonb NOT NULL DEFAULT '{}'::jsonb,
    metrics        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now()
);
"""

DOWNGRADE = r"""
DROP TABLE IF EXISTS eval_runs, eval_cases, usage_ledger, audit_log, jobs, feedback,
    message_citations, messages, chat_sessions, chunk_embeddings, embedding_generations,
    chunks, documents, ingestion_errors, ingestion_runs, sources, credentials, api_keys,
    sessions, group_members, groups, memberships, users, workspaces CASCADE;
DROP FUNCTION IF EXISTS chunks_copy_document_acl(), documents_propagate_acl(),
    chunk_embeddings_respect_scope();
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    # Extensions are left installed: other schemas may use them, and they're harmless.
    op.execute(DOWNGRADE)
