"""``users.last_active_at``: last activity that survives session and event cleanup (ADR-0042).

Sessions are deleted about a day after they expire and auth events after 90 days, so
deriving "last active" from them showed long-absent users as never active. The column is
written inside statements that already run (session create on sign-in, session touch at
most every 5 minutes), so it adds no round trips.

Additive; the backfill uses what is still on record. The downgrade drops the column.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE users ADD COLUMN last_active_at timestamptz;
        UPDATE users u SET last_active_at = x.t FROM (
            SELECT user_id, max(t) AS t FROM (
                SELECT user_id, last_seen_at AS t FROM sessions
                UNION ALL
                SELECT user_id, created_at FROM auth_events
                WHERE kind = 'login_success' AND user_id IS NOT NULL
            ) y GROUP BY user_id
        ) x WHERE x.user_id = u.id;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN last_active_at")
