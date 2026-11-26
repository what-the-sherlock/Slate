"""Read-only: list rows integration tests may have left behind (a run against the wrong
branch, or a test that died before its cleanup).

    uv run python scripts/find_test_leftovers.py

Uses DATABASE_URL_DIRECT. Changes nothing. Real accounts' workspaces have ``ws-...`` slugs;
test fixtures use the prefixes below and ``@it.test`` / ``@t.dev`` addresses.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Run from anywhere, like the other scripts: make the repo root importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from core.adapters.postgres.engine import (
    EngineKind,
    create_engine,
    create_session_factory,
    use_selector_loop_on_windows,
)
from core.adapters.postgres.uow import make_uow
from core.config import get_settings

WORKSPACES = (
    "SELECT slug, name, created_at FROM workspaces"
    " WHERE slug ~ '^(c|m|w|s|b|t|cat|ing|eval-it)-' ORDER BY created_at DESC LIMIT 200"
)
USERS = (
    "SELECT email, created_at FROM users WHERE email LIKE '%@it.test' OR email LIKE '%@t.dev'"
    " OR email LIKE '%@x.dev' OR email LIKE '%@example.com' ORDER BY created_at DESC LIMIT 200"
)


async def main() -> None:
    engine = create_engine(get_settings().require_database_url(direct=True), EngineKind.DIRECT)
    try:
        uow = make_uow(create_session_factory(engine))
        async with uow() as s:
            await s.execute(text("SET TRANSACTION READ ONLY"))
            ws = (await s.execute(text(WORKSPACES))).all()
            users = (await s.execute(text(USERS))).all()
        print(f"workspaces that look like test data: {len(ws)}")
        for slug, name, created in ws:
            print(f"  {created:%Y-%m-%d %H:%M}  {slug}  ({name})")
        print(f"users that look like test data: {len(users)}")
        for email, created in users:
            print(f"  {created:%Y-%m-%d %H:%M}  {email}")
        if not ws and not users:
            print("Nothing left behind.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    use_selector_loop_on_windows()
    asyncio.run(main())
