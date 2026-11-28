"""Deleting a chat (ADR-0044): soft delete with Undo, purged by the worker after 24 hours.

``deleted_at`` hides the chat from every read at once (the store's ownership filter adds
``deleted_at IS NULL``); restore clears it; the hourly housekeeping deletes rows older
than the undo window, and messages, citations and feedback cascade.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE chat_sessions ADD COLUMN deleted_at timestamptz;
        CREATE INDEX chat_sessions_deleted_idx ON chat_sessions (deleted_at)
            WHERE deleted_at IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM chat_sessions WHERE deleted_at IS NOT NULL;
        DROP INDEX chat_sessions_deleted_idx;
        ALTER TABLE chat_sessions DROP COLUMN deleted_at;
        """
    )
