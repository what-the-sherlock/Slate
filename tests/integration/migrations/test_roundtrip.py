"""Migrations are reversible: downgrade to base and upgrade back to head cleanly.

DESTRUCTIVE: drops every table on the target database, so it only runs when
SLATE_ALLOW_DESTRUCTIVE_TESTS=1 (set in CI for the dedicated test branch).
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.skipif(
    os.environ.get("SLATE_ALLOW_DESTRUCTIVE_TESTS") != "1",
    reason="drops all tables; set SLATE_ALLOW_DESTRUCTIVE_TESTS=1 on a disposable branch",
)


def alembic(*args: str) -> str:
    result = subprocess.run(  # noqa: S603 (fixed argv, no shell)
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout + result.stderr


def test_downgrade_to_base_and_back_to_head() -> None:
    alembic("downgrade", "base")
    assert "(head)" not in alembic("current")
    alembic("upgrade", "head")
    assert "(head)" in alembic("current")
