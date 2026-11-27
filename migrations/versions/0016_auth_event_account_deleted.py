"""The ``account_deleted`` auth event (ADR-0042).

When the admin deletes an account, the event outlives it: ``user_id`` becomes NULL (the
account is gone) and ``actor_id`` records which admin did it.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = (
    "'signup', 'email_verified', 'login_success', 'login_failed', 'login_throttled',"
    " 'logout', 'password_reset', 'account_disabled', 'account_enabled'"
)


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE auth_events DROP CONSTRAINT auth_events_kind_check;
        ALTER TABLE auth_events ADD CONSTRAINT auth_events_kind_check
            CHECK (kind IN ({_KINDS}, 'account_deleted'));
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DELETE FROM auth_events WHERE kind = 'account_deleted';
        ALTER TABLE auth_events DROP CONSTRAINT auth_events_kind_check;
        ALTER TABLE auth_events ADD CONSTRAINT auth_events_kind_check CHECK (kind IN ({_KINDS}));
        """
    )
