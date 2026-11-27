"""The ``rerank`` usage kind (IMPROVEMENT_PLAN P1-4, ADR-0040).

The optional selection step is a paid chat call, so it goes in the usage ledger like every
other one (ARCHITECTURE §12). Its kind needs a value the 0001 CHECK allows.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE usage_ledger DROP CONSTRAINT usage_ledger_kind_check;
        ALTER TABLE usage_ledger ADD CONSTRAINT usage_ledger_kind_check
            CHECK (kind IN ('embed', 'chat', 'rewrite', 'judge', 'rerank'));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE usage_ledger SET kind = 'chat' WHERE kind = 'rerank';
        ALTER TABLE usage_ledger DROP CONSTRAINT usage_ledger_kind_check;
        ALTER TABLE usage_ledger ADD CONSTRAINT usage_ledger_kind_check
            CHECK (kind IN ('embed', 'chat', 'rewrite', 'judge'));
        """
    )
