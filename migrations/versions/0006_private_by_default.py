"""Private by default; sharing is explicit; public data only in the eval workspace (ADR-0026).

1. ``workspaces.allows_public``: only the synthetic eval workspace (``slate-evals``) may hold
   public data. Triggers refuse a public source, or a ``public`` ACL entry on a source or
   document, anywhere else. So user data can never become eligible for free-tier AI
   providers (ADR-0012) or carry the ``public`` principal, whatever the application does.
2. ``sources.created_by``: who connected a source. Connected sources are readable by that
   user only (``user:<id>``) until they deliberately share them.
3. Data migration:
   - GitHub sources (``workspace:<ws>`` until now) become ``user:<workspace owner>``. Today
     every workspace has exactly one member, its owner, so nobody loses access; the change
     stops a future invite from silently sharing every repo.
   - "Public uploads" outside the eval workspace are merged into the private "Uploads"
     source with the ``public`` entry removed (they stay readable by the workspace, as
     before), and their free-tier (public-scoped) embeddings are deleted.

Forward-only for the data: the downgrade drops the new columns and triggers but does not
re-publish anything.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = """
ALTER TABLE workspaces ADD COLUMN allows_public boolean NOT NULL DEFAULT false;
UPDATE workspaces SET allows_public = true WHERE slug = 'slate-evals';

ALTER TABLE sources ADD COLUMN created_by uuid REFERENCES users(id) ON DELETE SET NULL;
CREATE INDEX sources_created_by_idx ON sources (created_by) WHERE created_by IS NOT NULL;
"""

_GITHUB_TO_CONNECTOR = """
UPDATE sources s SET created_by = (
    SELECT m.user_id FROM memberships m
    WHERE m.workspace_id = s.workspace_id AND m.role = 'owner'
    ORDER BY m.created_at, m.user_id LIMIT 1)
WHERE s.connector <> 'upload' AND s.created_by IS NULL;

UPDATE sources SET visibility = ARRAY['user:' || created_by::text]
WHERE connector = 'github' AND created_by IS NOT NULL;

-- documents.acl -> chunks.acl follows by the existing trigger (documents_acl_to_chunks).
UPDATE documents d SET acl = s.visibility, updated_at = now()
FROM sources s
WHERE d.source_id = s.id AND s.connector = 'github' AND s.created_by IS NOT NULL
  AND d.acl IS DISTINCT FROM s.visibility;
"""

_UNPUBLISH_USER_DATA = """
-- Free-tier embeddings of chunks outside the eval workspace.
DELETE FROM chunk_embeddings e
USING chunks c, workspaces w, embedding_generations g
WHERE e.chunk_id = c.id AND c.workspace_id = w.id AND e.generation_id = g.id
  AND g.data_scope = 'public' AND NOT w.allows_public;

-- A private "Uploads" source for every workspace that is about to need one.
INSERT INTO sources (workspace_id, connector, name, visibility, data_class)
SELECT DISTINCT s.workspace_id, 'upload', 'Uploads',
       ARRAY['workspace:' || s.workspace_id::text], 'private'
FROM sources s JOIN workspaces w ON w.id = s.workspace_id
WHERE s.connector = 'upload' AND s.data_class = 'public' AND NOT w.allows_public
ON CONFLICT (workspace_id, name) DO NOTHING;

-- Refuse to continue (never delete) if a workspace has public uploads but no private
-- "Uploads" upload source to move them into (e.g. the name taken by something else).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM sources s JOIN workspaces w ON w.id = s.workspace_id
        WHERE s.connector = 'upload' AND s.data_class = 'public' AND NOT w.allows_public
          AND NOT EXISTS (SELECT 1 FROM sources u WHERE u.workspace_id = s.workspace_id
                          AND u.name = 'Uploads' AND u.connector = 'upload'
                          AND u.data_class = 'private'))
    THEN
        RAISE EXCEPTION '0006: a workspace has public uploads but no private Uploads source';
    END IF;
END $$;

-- The same file can't be in both upload sources (dedup refuses that), but if it ever is,
-- the private copy wins and the public duplicate goes.
DELETE FROM documents d
USING sources s, workspaces w, documents p, sources u
WHERE d.source_id = s.id AND s.connector = 'upload' AND s.data_class = 'public'
  AND w.id = s.workspace_id AND NOT w.allows_public
  AND u.workspace_id = s.workspace_id AND u.connector = 'upload' AND u.name = 'Uploads'
  AND p.source_id = u.id AND p.external_id = d.external_id;

-- Move the rest into "Uploads" as private data. The chunks FK cascades the data class and
-- the ACL trigger copies the new ACL onto chunks.
UPDATE documents d SET
    source_id = u.id,
    data_class = 'private',
    acl = CASE WHEN cardinality(array_remove(d.acl, 'public')) = 0
               THEN ARRAY['workspace:' || d.workspace_id::text]
               ELSE array_remove(d.acl, 'public') END,
    updated_at = now()
FROM sources s, workspaces w, sources u
WHERE d.source_id = s.id AND s.connector = 'upload' AND s.data_class = 'public'
  AND w.id = s.workspace_id AND NOT w.allows_public
  AND u.workspace_id = s.workspace_id AND u.connector = 'upload' AND u.name = 'Uploads'
  AND u.data_class = 'private';

DELETE FROM sources s USING workspaces w
WHERE w.id = s.workspace_id AND NOT w.allows_public
  AND s.connector = 'upload' AND s.data_class = 'public';

-- Any other public source outside the eval workspace (none are expected): make it
-- private in place. The FKs cascade the class to documents and chunks first.
UPDATE sources s SET data_class = 'private' FROM workspaces w
WHERE w.id = s.workspace_id AND NOT w.allows_public AND s.data_class = 'public';

UPDATE sources s SET visibility = CASE
        WHEN cardinality(array_remove(s.visibility, 'public')) = 0
        THEN ARRAY['workspace:' || s.workspace_id::text]
        ELSE array_remove(s.visibility, 'public') END
FROM workspaces w
WHERE w.id = s.workspace_id AND NOT w.allows_public AND 'public' = ANY (s.visibility);

UPDATE documents d SET acl = CASE
        WHEN cardinality(array_remove(d.acl, 'public')) = 0
        THEN ARRAY['workspace:' || d.workspace_id::text]
        ELSE array_remove(d.acl, 'public') END,
    updated_at = now()
FROM workspaces w
WHERE w.id = d.workspace_id AND NOT w.allows_public AND 'public' = ANY (d.acl);
"""

_GUARDS = """
-- Public data, or the 'public' principal, only where the workspace allows it.
CREATE FUNCTION sources_public_only_where_allowed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.data_class = 'public' OR 'public' = ANY (NEW.visibility))
       AND NOT EXISTS (SELECT 1 FROM workspaces w
                       WHERE w.id = NEW.workspace_id AND w.allows_public) THEN
        RAISE EXCEPTION 'public data is not allowed in workspace %', NEW.workspace_id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'sources_public_only_where_allowed';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sources_public_only_where_allowed
    BEFORE INSERT OR UPDATE OF data_class, visibility, workspace_id ON sources
    FOR EACH ROW EXECUTE FUNCTION sources_public_only_where_allowed();

-- chunks.acl is a trigger-maintained copy of documents.acl, so guarding documents is
-- enough; chunks.data_class follows documents through the composite FK.
CREATE FUNCTION documents_public_only_where_allowed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF 'public' = ANY (NEW.acl)
       AND NOT EXISTS (SELECT 1 FROM workspaces w
                       WHERE w.id = NEW.workspace_id AND w.allows_public) THEN
        RAISE EXCEPTION 'public ACL is not allowed in workspace %', NEW.workspace_id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'documents_public_only_where_allowed';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER documents_public_only_where_allowed
    BEFORE INSERT OR UPDATE OF acl, workspace_id ON documents
    FOR EACH ROW EXECUTE FUNCTION documents_public_only_where_allowed();

-- chunks.acl is trigger-maintained, but refuse a direct write of 'public' too.
CREATE FUNCTION chunks_public_only_where_allowed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF 'public' = ANY (NEW.acl)
       AND NOT EXISTS (SELECT 1 FROM workspaces w
                       WHERE w.id = NEW.workspace_id AND w.allows_public) THEN
        RAISE EXCEPTION 'public ACL is not allowed in workspace %', NEW.workspace_id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'chunks_public_only_where_allowed';
    END IF;
    RETURN NEW;
END $$;
-- Named to sort after chunks_acl_from_document, so it checks the final copied ACL.
CREATE TRIGGER chunks_zz_public_only_where_allowed
    BEFORE INSERT OR UPDATE OF acl, workspace_id ON chunks
    FOR EACH ROW EXECUTE FUNCTION chunks_public_only_where_allowed();

-- A workspace can't stop allowing public data while it still holds some.
CREATE FUNCTION workspaces_keep_allows_public() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM sources s WHERE s.workspace_id = NEW.id
               AND (s.data_class = 'public' OR 'public' = ANY (s.visibility)))
       OR EXISTS (SELECT 1 FROM documents d WHERE d.workspace_id = NEW.id
                  AND 'public' = ANY (d.acl)) THEN
        RAISE EXCEPTION 'workspace % still holds public data', NEW.id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'workspaces_keep_allows_public';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER workspaces_keep_allows_public
    BEFORE UPDATE OF allows_public ON workspaces
    FOR EACH ROW WHEN (OLD.allows_public AND NOT NEW.allows_public)
    EXECUTE FUNCTION workspaces_keep_allows_public();
"""


def upgrade() -> None:
    op.execute(_SCHEMA)
    op.execute(_GITHUB_TO_CONNECTOR)
    op.execute(_UNPUBLISH_USER_DATA)
    op.execute(_GUARDS)


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS workspaces_keep_allows_public ON workspaces;
        DROP FUNCTION IF EXISTS workspaces_keep_allows_public();
        DROP TRIGGER IF EXISTS chunks_zz_public_only_where_allowed ON chunks;
        DROP FUNCTION IF EXISTS chunks_public_only_where_allowed();
        DROP TRIGGER IF EXISTS documents_public_only_where_allowed ON documents;
        DROP FUNCTION IF EXISTS documents_public_only_where_allowed();
        DROP TRIGGER IF EXISTS sources_public_only_where_allowed ON sources;
        DROP FUNCTION IF EXISTS sources_public_only_where_allowed();
        DROP INDEX IF EXISTS sources_created_by_idx;
        ALTER TABLE sources DROP COLUMN IF EXISTS created_by;
        ALTER TABLE workspaces DROP COLUMN IF EXISTS allows_public;
        """
    )
