"""Open sign-up (ADR-0021): drop the guest, add canonical emails, verification and codes.

* The guest user goes (its memberships and sessions cascade). Every account now has a
  password, so ``users_login_method`` is replaced by ``password_hash NOT NULL``.
* ``email_canonical`` makes one inbox one account (``core.domain.auth.canonical_email``;
  the SQL below computes the same thing for existing rows).
* ``email_verified_at``: existing accounts were created through owner setup, so they
  count as verified. New ones start NULL and can't spend until verified.
* ``email_codes``: at most one live code per user and purpose, stored as an HMAC.
* One membership per user, until invites exist (M7): a session resolves to exactly one
  workspace.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM users WHERE is_guest;
        ALTER TABLE users DROP CONSTRAINT users_login_method;
        ALTER TABLE users DROP COLUMN is_guest;
        ALTER TABLE users ALTER COLUMN password_hash SET NOT NULL;

        ALTER TABLE users ADD COLUMN email_canonical citext;
        UPDATE users SET email_canonical = (
            WITH parts AS (
                SELECT split_part(split_part(lower(email), '@', 1), '+', 1) AS local,
                       split_part(lower(email), '@', 2) AS domain
            )
            SELECT CASE WHEN domain IN ('gmail.com', 'googlemail.com')
                        THEN replace(local, '.', '') || '@gmail.com'
                        ELSE local || '@' || domain END
            FROM parts
        );
        ALTER TABLE users ALTER COLUMN email_canonical SET NOT NULL;
        CREATE UNIQUE INDEX users_email_canonical_key ON users (email_canonical);

        ALTER TABLE users ADD COLUMN email_verified_at timestamptz;
        UPDATE users SET email_verified_at = created_at;
        -- The worker sweep finds stale unverified accounts through this.
        CREATE INDEX users_unverified_idx ON users (created_at) WHERE email_verified_at IS NULL;

        CREATE UNIQUE INDEX memberships_one_per_user ON memberships (user_id);

        CREATE TABLE email_codes (
            user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            purpose      text NOT NULL CHECK (purpose IN ('verify_email', 'reset_password')),
            code_hash    bytea NOT NULL CHECK (octet_length(code_hash) = 32),
            expires_at   timestamptz NOT NULL,
            attempts     integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            consumed_at  timestamptz,
            created_at   timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, purpose)
        );
        """
    )


def downgrade() -> None:
    # Schema only: the deleted guest row isn't recreated (sign up or set up again).
    op.execute(
        """
        DROP TABLE IF EXISTS email_codes;
        DROP INDEX IF EXISTS memberships_one_per_user;
        DROP INDEX IF EXISTS users_unverified_idx;
        ALTER TABLE users DROP COLUMN IF EXISTS email_verified_at;
        DROP INDEX IF EXISTS users_email_canonical_key;
        ALTER TABLE users DROP COLUMN IF EXISTS email_canonical;
        ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL;
        ALTER TABLE users ADD COLUMN is_guest boolean NOT NULL DEFAULT false;
        ALTER TABLE users ADD CONSTRAINT users_login_method
            CHECK (is_guest OR password_hash IS NOT NULL);
        """
    )
