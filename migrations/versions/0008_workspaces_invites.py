"""Shared workspaces: several per account, invitations, explicit sharing (ADR-0030).

* ``memberships_one_per_user`` goes: an account keeps its personal workspace and may join
  others by invitation.
* ``sessions.workspace_id`` is the *active* workspace. It's only a preference: every request
  re-joins ``memberships``, so a removed member falls back to their own workspace at once.
* ``invitations``: a single-use token (stored as SHA-256) for one email address and role,
  14 days. At most one open invite per (workspace, address).
* ``audit_log`` already exists (0001); invites, role changes, removals and sharing write to it.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS memberships_one_per_user;

        ALTER TABLE sessions
            ADD COLUMN workspace_id uuid REFERENCES workspaces(id) ON DELETE SET NULL;

        CREATE TABLE invitations (
            id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id     uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            email_canonical  citext NOT NULL,
            role             text NOT NULL CHECK (role IN ('admin', 'member', 'viewer')),
            token_hash       bytea NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
            invited_by       uuid REFERENCES users(id) ON DELETE SET NULL,
            created_at       timestamptz NOT NULL DEFAULT now(),
            expires_at       timestamptz NOT NULL,
            accepted_at      timestamptz,
            revoked_at       timestamptz,
            CHECK (accepted_at IS NULL OR revoked_at IS NULL)
        );
        CREATE UNIQUE INDEX invitations_one_open
            ON invitations (workspace_id, email_canonical)
            WHERE accepted_at IS NULL AND revoked_at IS NULL;
        CREATE INDEX invitations_workspace_idx ON invitations (workspace_id, created_at DESC);

        CREATE INDEX IF NOT EXISTS memberships_workspace_idx ON memberships (workspace_id);

        -- A connector source's documents always carry the source's *current* visibility, so a
        -- sync that loaded the source before someone (un)shared it can't write a stale ACL.
        -- Uploads keep their per-file ACL. chunks.acl follows documents by the 0001 trigger.
        CREATE FUNCTION documents_acl_from_source() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.acl := coalesce(
                (SELECT s.visibility FROM sources s
                 WHERE s.id = NEW.source_id AND s.connector <> 'upload'),
                NEW.acl);
            RETURN NEW;
        END $$;
        CREATE TRIGGER documents_acl_from_source
            BEFORE INSERT OR UPDATE OF acl, source_id ON documents
            FOR EACH ROW EXECUTE FUNCTION documents_acl_from_source();
        """
    )


def downgrade() -> None:
    # Re-adding one-workspace-per-user fails while someone belongs to two: that's on purpose
    # (a downgrade must not silently drop memberships).
    op.execute(
        """
        DROP TRIGGER IF EXISTS documents_acl_from_source ON documents;
        DROP FUNCTION IF EXISTS documents_acl_from_source();
        DROP INDEX IF EXISTS memberships_workspace_idx;
        DROP TABLE IF EXISTS invitations;
        ALTER TABLE sessions DROP COLUMN IF EXISTS workspace_id;
        CREATE UNIQUE INDEX memberships_one_per_user ON memberships (user_id);
        """
    )
