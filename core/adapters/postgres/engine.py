"""Async SQLAlchemy engines for Neon.

Two shapes, because the two callers run very differently:

* ``pooled`` (API on Vercel): every request may land on a fresh process, so we use
  ``NullPool`` and let Neon's PgBouncer do the pooling. PgBouncer runs in
  *transaction* mode, which hands the server connection to another client between
  transactions, so session-scoped server-side prepared statements must be disabled
  (``prepare_threshold=None``).
* ``direct`` (worker, migrations, scripts): long-lived process on the non-pooled
  endpoint with a small ordinary pool.
"""

from __future__ import annotations

import asyncio
import sys
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

CONNECT_TIMEOUT_S = 5


class EngineKind(StrEnum):
    POOLED = "pooled"
    DIRECT = "direct"


def use_selector_loop_on_windows() -> None:
    """psycopg's async mode can't run on Windows' default Proactor event loop.

    Call before starting an event loop in local entry points and tests. A no-op on
    Linux (Vercel, GitHub Actions).
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # pyright: ignore[reportDeprecated]


def create_engine(url: str, kind: EngineKind, *, echo: bool = False) -> AsyncEngine:
    connect_args: dict[str, Any] = {  # reason: driver-specific kwargs passed to psycopg
        "connect_timeout": CONNECT_TIMEOUT_S,
        "application_name": f"slate-{kind.value}",
    }
    if kind is EngineKind.POOLED:
        connect_args["prepare_threshold"] = None
        return create_async_engine(url, poolclass=NullPool, connect_args=connect_args, echo=echo)
    return create_async_engine(
        url,
        pool_size=3,
        max_overflow=2,
        pool_pre_ping=True,  # Neon scales to zero; detect connections it dropped
        pool_recycle=240,  # recycle before Neon's 5-minute idle suspend
        connect_args=connect_args,
        echo=echo,
    )


def create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[Any]:  # reason: AsyncSession generic
    return async_sessionmaker(engine, expire_on_commit=False)
