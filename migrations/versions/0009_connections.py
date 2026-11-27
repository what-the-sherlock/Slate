"""Integrations: signed-in provider accounts (ADR-0032).

``connections`` holds one row per (workspace, user, provider, provider account):
* tokens are one Fernet ciphertext (``secret_encrypted``; the key lives only in env), so
  a refresh replaces token, expiry and status in one UPDATE;
* ``status`` is what the Integrations page shows (connected, needs_reconnect, error);
* the row belongs to a *membership*: the composite foreign key cascades, so leaving or
  being removed from a workspace (ADR-0030) deletes that person's connections there, in
  the database, whatever the application does;
* ``refresh_lease_until`` lets one refresher at a time rotate a refresh token.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE connections (
            id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id         uuid NOT NULL,
            user_id              uuid NOT NULL,
            provider             text NOT NULL CHECK (provider ~ '^[a-z][a-z0-9_]{1,31}$'),
            account_id           text NOT NULL CHECK (length(account_id) BETWEEN 1 AND 200),
            account_label        text CHECK (length(account_label) <= 200),
            scopes               text[] NOT NULL DEFAULT '{}',
            status               text NOT NULL DEFAULT 'connected'
                                 CHECK (status IN ('connected', 'needs_reconnect', 'error')),
            secret_encrypted     bytea NOT NULL,
            expires_at           timestamptz,
            last_error           text CHECK (length(last_error) <= 300),
            refresh_lease_until  timestamptz,
            last_refreshed_at    timestamptz,
            created_at           timestamptz NOT NULL DEFAULT now(),
            updated_at           timestamptz NOT NULL DEFAULT now(),
            FOREIGN KEY (workspace_id, user_id)
                REFERENCES memberships (workspace_id, user_id) ON DELETE CASCADE,
            UNIQUE (workspace_id, user_id, provider, account_id)
        );
        CREATE INDEX connections_owner_idx ON connections (workspace_id, user_id);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS connections;")
