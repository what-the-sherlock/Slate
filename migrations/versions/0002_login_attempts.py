"""Login attempts for throttling (no Redis: Postgres holds the counters).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE login_attempts (
            id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            email       citext NOT NULL,
            ip          inet,
            success     boolean NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX login_attempts_email_idx ON login_attempts (email, created_at)
            WHERE NOT success;
        CREATE INDEX login_attempts_ip_idx ON login_attempts (ip, created_at)
            WHERE NOT success;
        CREATE INDEX login_attempts_created_idx ON login_attempts (created_at);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS login_attempts")
