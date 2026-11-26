"""Email sources (ADR-0028): a live IMAP connector next to the .mbox import.

* ``sources.connector`` gains ``imap``.
* Email is always private data: the 0001 rule for ``gmail_mbox`` now covers ``imap`` too.
* A source's credential is deleted with the source (Fernet ciphertext of an app password
  must not outlive the connection): ``credentials.id`` is referenced with ON DELETE SET NULL
  from sources, so a trigger removes the orphaned credential when its source goes.
* One email connection per mailbox per workspace: a partial unique index on the address.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE sources DROP CONSTRAINT IF EXISTS sources_connector_check;
        ALTER TABLE sources ADD CONSTRAINT sources_connector_check
            CHECK (connector IN ('upload', 'github', 'discord', 'gmail_mbox', 'imap'));

        ALTER TABLE sources DROP CONSTRAINT sources_gmail_always_private;
        ALTER TABLE sources ADD CONSTRAINT sources_email_always_private
            CHECK (connector NOT IN ('gmail_mbox', 'imap') OR data_class = 'private');

        CREATE UNIQUE INDEX sources_one_imap_per_mailbox
            ON sources (workspace_id, lower(config ->> 'address'))
            WHERE connector = 'imap';

        CREATE FUNCTION sources_delete_credential() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.credential_id IS NOT NULL THEN
                DELETE FROM credentials WHERE id = OLD.credential_id
                  AND NOT EXISTS (SELECT 1 FROM sources s
                                  WHERE s.credential_id = OLD.credential_id AND s.id <> OLD.id);
            END IF;
            RETURN OLD;
        END $$;
        CREATE TRIGGER sources_delete_credential
            AFTER DELETE ON sources
            FOR EACH ROW EXECUTE FUNCTION sources_delete_credential();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS sources_delete_credential ON sources;
        DROP FUNCTION IF EXISTS sources_delete_credential();
        DROP INDEX IF EXISTS sources_one_imap_per_mailbox;
        ALTER TABLE sources DROP CONSTRAINT IF EXISTS sources_email_always_private;
        ALTER TABLE sources ADD CONSTRAINT sources_gmail_always_private
            CHECK (connector <> 'gmail_mbox' OR data_class = 'private');
        ALTER TABLE sources DROP CONSTRAINT IF EXISTS sources_connector_check;
        ALTER TABLE sources ADD CONSTRAINT sources_connector_check
            CHECK (connector IN ('upload', 'github', 'discord', 'gmail_mbox'));
        """
    )
