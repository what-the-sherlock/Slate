"""Regenerate and edit by superseding (ADR-0024).

Regenerating an answer or editing a question replaces a turn and everything after it.
The replaced messages are marked ``superseded_at`` instead of deleted: history and the
prompt see one linear conversation, while feedback and audit rows survive (and branch
navigation stays possible later). Reads filter ``superseded_at IS NULL``, so the live
rows get their own partial index.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE messages ADD COLUMN superseded_at timestamptz;
        CREATE INDEX messages_live_idx ON messages (session_id, created_at)
            WHERE superseded_at IS NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS messages_live_idx;
        ALTER TABLE messages DROP COLUMN IF EXISTS superseded_at;
        """
    )
