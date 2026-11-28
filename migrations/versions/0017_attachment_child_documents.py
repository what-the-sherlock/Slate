"""Attachment child documents (ADR-0043).

An attachment's text is a document of its own whose ``parent_document_id`` is the email
or message it came with. The database, not the services, keeps it no more visible than
its parent:

* a composite FK puts the child in the parent's workspace and data class and deletes it
  with the parent (and follows a data-class change);
* the child must share the parent's source, and only one level is allowed (a trigger);
* the child's ``acl`` and ``deleted_at`` are copied from the parent on every insert and
  update, and pushed down when the parent's change (chunks already copy their
  document's ACL, so the child's chunks follow too).

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE documents ADD COLUMN parent_document_id uuid;
        ALTER TABLE documents ADD CONSTRAINT documents_not_own_parent
            CHECK (parent_document_id IS DISTINCT FROM id);
        ALTER TABLE documents ADD CONSTRAINT documents_parent_fk
            FOREIGN KEY (parent_document_id, workspace_id, data_class)
            REFERENCES documents (id, workspace_id, data_class)
            ON UPDATE CASCADE ON DELETE CASCADE;
        CREATE INDEX documents_parent_idx ON documents (parent_document_id)
            WHERE parent_document_id IS NOT NULL;

        -- A child copies its parent's access and deletion; it can't choose its own.
        CREATE FUNCTION documents_inherit_from_parent() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE p record;
        BEGIN
            IF NEW.parent_document_id IS NULL THEN
                RETURN NEW;
            END IF;
            SELECT source_id, acl, deleted_at, parent_document_id INTO p
                FROM documents WHERE id = NEW.parent_document_id;
            IF NOT FOUND THEN
                RETURN NEW;  -- the FK reports the missing parent
            END IF;
            IF p.parent_document_id IS NOT NULL THEN
                RAISE EXCEPTION 'attachment documents cannot have attachments'
                    USING ERRCODE = 'check_violation', CONSTRAINT = 'documents_one_level';
            END IF;
            IF p.source_id <> NEW.source_id THEN
                RAISE EXCEPTION 'an attachment must belong to its parent''s source'
                    USING ERRCODE = 'check_violation', CONSTRAINT = 'documents_parent_source';
            END IF;
            NEW.acl := p.acl;
            NEW.deleted_at := p.deleted_at;
            RETURN NEW;
        END $$;
        CREATE TRIGGER documents_child_inherits
            BEFORE INSERT OR UPDATE OF parent_document_id, acl, deleted_at, source_id
            ON documents FOR EACH ROW EXECUTE FUNCTION documents_inherit_from_parent();

        CREATE FUNCTION documents_propagate_to_children() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE documents SET acl = NEW.acl, deleted_at = NEW.deleted_at
                WHERE parent_document_id = NEW.id;
            RETURN NULL;
        END $$;
        CREATE TRIGGER documents_parent_to_children
            AFTER UPDATE OF acl, deleted_at ON documents FOR EACH ROW
            WHEN (OLD.acl IS DISTINCT FROM NEW.acl OR OLD.deleted_at IS DISTINCT FROM NEW.deleted_at)
            EXECUTE FUNCTION documents_propagate_to_children();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER documents_parent_to_children ON documents;
        DROP FUNCTION documents_propagate_to_children();
        DROP TRIGGER documents_child_inherits ON documents;
        DROP FUNCTION documents_inherit_from_parent();
        DELETE FROM documents WHERE parent_document_id IS NOT NULL;
        DROP INDEX documents_parent_idx;
        ALTER TABLE documents DROP CONSTRAINT documents_parent_fk;
        ALTER TABLE documents DROP CONSTRAINT documents_not_own_parent;
        ALTER TABLE documents DROP COLUMN parent_document_id;
        """
    )
