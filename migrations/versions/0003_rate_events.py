"""Generic rate-limit events (uploads now; chat and guest usage later). No Redis needed.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE rate_events (
            id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            key         text NOT NULL,
            cost        bigint NOT NULL DEFAULT 1 CHECK (cost >= 0),
            created_at  timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX rate_events_key_idx ON rate_events (key, created_at);
        CREATE INDEX rate_events_created_idx ON rate_events (created_at);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS rate_events")
