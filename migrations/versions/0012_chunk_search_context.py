"""Titles and metadata in keyword search (IMPROVEMENT_PLAN P1-3, ADR-0037).

* ``chunks.search_context``: the document's title, author and a few metadata values
  (``core.domain.chunking.search_context``), written with every chunk.
* ``tsv_en`` and ``tsv_simple`` are regenerated to include it at weight A, with the body
  left at the default weight D, so a title match ranks above a passing mention.

Generated columns can't change their expression in place before PostgreSQL 17, so both
are dropped and re-added. That drops their GIN indexes, which are recreated. Dropping
first means the backfill rewrites each row only once. The table is rewritten under an
ACCESS EXCLUSIVE lock: seconds on this corpus, but run it at a quiet time.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The same text core.domain.chunking.search_context builds (title, author, then the
# SEARCH_CONTEXT_KEYS values), for the chunks that already exist.
_BACKFILL = """
UPDATE chunks c SET search_context = left(concat_ws(' ',
        nullif(btrim(d.title), ''), nullif(btrim(d.author), ''),
        nullif(btrim(d.metadata->>'repo'), ''), nullif(btrim(d.metadata->>'path'), ''),
        nullif(btrim(d.metadata->>'state'), ''), nullif(btrim(d.metadata->>'identifier'), '')
    ), 500)
FROM documents d WHERE d.id = c.document_id
"""


def upgrade() -> None:
    op.execute("ALTER TABLE chunks DROP COLUMN tsv_en, DROP COLUMN tsv_simple")
    op.execute("ALTER TABLE chunks ADD COLUMN search_context text NOT NULL DEFAULT ''")
    op.execute(_BACKFILL)
    op.execute(
        """
        ALTER TABLE chunks
            ADD COLUMN tsv_en tsvector GENERATED ALWAYS AS (
                setweight(to_tsvector('english', search_context), 'A')
                || to_tsvector('english', coalesce(heading_path, '') || ' ' || content)
            ) STORED,
            ADD COLUMN tsv_simple tsvector GENERATED ALWAYS AS (
                setweight(to_tsvector('simple', search_context), 'A')
                || to_tsvector('simple', content)
            ) STORED
        """
    )
    op.execute("CREATE INDEX chunks_tsv_en_gin ON chunks USING gin (tsv_en)")
    op.execute("CREATE INDEX chunks_tsv_simple_gin ON chunks USING gin (tsv_simple)")


def downgrade() -> None:
    op.execute("ALTER TABLE chunks DROP COLUMN tsv_en, DROP COLUMN tsv_simple")
    op.execute("ALTER TABLE chunks DROP COLUMN search_context")
    op.execute(
        """
        ALTER TABLE chunks
            ADD COLUMN tsv_en tsvector GENERATED ALWAYS AS
                (to_tsvector('english', coalesce(heading_path, '') || ' ' || content)) STORED,
            ADD COLUMN tsv_simple tsvector GENERATED ALWAYS AS
                (to_tsvector('simple', content)) STORED
        """
    )
    op.execute("CREATE INDEX chunks_tsv_en_gin ON chunks USING gin (tsv_en)")
    op.execute("CREATE INDEX chunks_tsv_simple_gin ON chunks USING gin (tsv_simple)")
