"""Platform-level auth events for the admin console (ADR-0042).

Append-only: what happened, to which account, by whom, when. Deliberately no IP, user
agent or email (SECURITY.md): the console needs counts and timelines, not tracking data.
Platform-wide, so there is no workspace_id (the one exception to workspace scoping, like
``users`` itself). Rows older than 90 days are deleted by the worker's hourly sweep.

Purely additive; the downgrade drops the table.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE auth_events (
            id          bigserial PRIMARY KEY,
            kind        text NOT NULL CHECK (kind IN (
                'signup', 'email_verified', 'login_success', 'login_failed',
                'login_throttled', 'logout', 'password_reset',
                'account_disabled', 'account_enabled')),
            user_id     uuid REFERENCES users(id) ON DELETE SET NULL,
            actor_id    uuid REFERENCES users(id) ON DELETE SET NULL,
            created_at  timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX auth_events_created_idx ON auth_events (created_at);
        CREATE INDEX auth_events_user_idx ON auth_events (user_id, created_at)
            WHERE user_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE auth_events")
