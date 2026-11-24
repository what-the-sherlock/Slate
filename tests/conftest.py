from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Mapping

import pytest
import pytest_socket


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]] | None:
    """psycopg async can't use Windows' Proactor loop, so tests use the Selector loop.

    Returning None on Linux keeps pytest-asyncio's default loop (CI is unaffected).
    """
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return None


@pytest.hookimpl(trylast=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Integration tests talk to the Neon test branch, so lift the socket guard for them."""
    if item.get_closest_marker("integration"):
        pytest_socket._remove_restrictions()
