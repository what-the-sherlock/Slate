"""Skipped-item accounting on ingestion runs (P0-4), and the ``import`` run kind.

* ``ingestion_runs.docs_skipped``: how many items a run saw but didn't index.
* ``ingestion_runs.skipped``: why, as a flat JSON map ``{"reason[:kind]": count}``
  (``core.domain.skips``). Bounded in code to 24 keys; the CHECK keeps it an object.
* ``kind`` gains ``import``: the mbox import has always created ``import`` runs, which the
  0001 CHECK rejected, so every import failed on Postgres before it started.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE ingestion_runs
            ADD COLUMN docs_skipped integer NOT NULL DEFAULT 0 CHECK (docs_skipped >= 0),
            ADD COLUMN skipped jsonb NOT NULL DEFAULT '{}'::jsonb
                CHECK (jsonb_typeof(skipped) = 'object');

        ALTER TABLE ingestion_runs DROP CONSTRAINT ingestion_runs_kind_check;
        ALTER TABLE ingestion_runs ADD CONSTRAINT ingestion_runs_kind_check
            CHECK (kind IN ('full', 'incremental', 'prune', 'upload', 'webhook', 'import'));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM ingestion_runs WHERE kind = 'import';
        ALTER TABLE ingestion_runs DROP CONSTRAINT ingestion_runs_kind_check;
        ALTER TABLE ingestion_runs ADD CONSTRAINT ingestion_runs_kind_check
            CHECK (kind IN ('full', 'incremental', 'prune', 'upload', 'webhook'));
        ALTER TABLE ingestion_runs DROP COLUMN skipped, DROP COLUMN docs_skipped;
        """
    )
