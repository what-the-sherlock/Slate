"""Connected sources: sources that belong to an OAuth connection (ADR-0033).

* ``sources.connection_id``: an OAuth-backed source (Gmail, Notion, Linear, Google Calendar,
  Google Drive, Slack) belongs to one connection (ADR-0032), at most one source per
  connection. ``ON DELETE CASCADE``: disconnecting deletes the source and, through the
  existing cascades, its documents, chunks, embeddings and runs, in one statement. Leaving a
  workspace already deletes the person's connections (0009), so their sources go too.
* The new connector names, each of which must have its connection.
* Email (``gmail``, ``imap``, ``gmail_mbox``) stays always-private data (ADR-0012, ADR-0026).

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONNECTED = "'gmail', 'notion', 'linear', 'google_calendar', 'google_drive', 'slack'"


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE sources
            ADD COLUMN connection_id uuid REFERENCES connections (id) ON DELETE CASCADE;
        CREATE UNIQUE INDEX sources_one_per_connection
            ON sources (connection_id) WHERE connection_id IS NOT NULL;

        ALTER TABLE sources DROP CONSTRAINT IF EXISTS sources_connector_check;
        ALTER TABLE sources ADD CONSTRAINT sources_connector_check
            CHECK (connector IN ('upload', 'github', 'discord', 'gmail_mbox', 'imap', {_CONNECTED}));

        ALTER TABLE sources DROP CONSTRAINT sources_email_always_private;
        ALTER TABLE sources ADD CONSTRAINT sources_email_always_private
            CHECK (connector NOT IN ('gmail_mbox', 'imap', 'gmail') OR data_class = 'private');

        ALTER TABLE sources ADD CONSTRAINT sources_connected_has_connection
            CHECK (connector NOT IN ({_CONNECTED}) OR connection_id IS NOT NULL);
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DELETE FROM sources WHERE connector IN ({_CONNECTED});
        ALTER TABLE sources DROP CONSTRAINT IF EXISTS sources_connected_has_connection;
        ALTER TABLE sources DROP CONSTRAINT sources_email_always_private;
        ALTER TABLE sources ADD CONSTRAINT sources_email_always_private
            CHECK (connector NOT IN ('gmail_mbox', 'imap') OR data_class = 'private');
        ALTER TABLE sources DROP CONSTRAINT sources_connector_check;
        ALTER TABLE sources ADD CONSTRAINT sources_connector_check
            CHECK (connector IN ('upload', 'github', 'discord', 'gmail_mbox', 'imap'));
        DROP INDEX IF EXISTS sources_one_per_connection;
        ALTER TABLE sources DROP COLUMN IF EXISTS connection_id;
        """
    )
