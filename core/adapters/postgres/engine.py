"""Async SQLAlchemy engines for Neon.

Two shapes, because the two callers run very differently:

* ``pooled`` (API on Vercel): Neon's pooled (PgBouncer) endpoint plus a *small*
  client-side pool, so a warm process reuses connections instead of paying a
  TCP + TLS + SCRAM handshake (~1.3 s from the dev laptop to us-east-1) for every
  unit of work (ADR-0018). PgBouncer runs in *transaction* mode, which hands the
  server connection to another client between transactions, so session-scoped
  server-side prepared statements must stay disabled (``prepare_threshold=None``).
* ``direct`` (worker, migrations, scripts): long-lived process on the non-pooled
  endpoint with a small ordinary pool.
"""

from __future__ import annotations

import asyncio
import sys
import time
from enum import StrEnum
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from core.request_metrics import record_connect

CONNECT_TIMEOUT_S = 5

# API pool (ADR-0018). Small: Neon's pooler multiplexes, and each Vercel instance
# serves few concurrent requests. Recycle well inside Neon's 5-minute idle suspend.
POOLED_POOL_SIZE = 2
POOLED_MAX_OVERFLOW = 3
POOLED_CHECKOUT_TIMEOUT_S = 5
POOLED_RECYCLE_S = 120


def _instrument(engine: AsyncEngine) -> None:
    """Time every new physical connection so the access log can report it."""

    @event.listens_for(engine.sync_engine, "do_connect")
    def _timed_connect(
        dialect: Any,  # reason: SQLAlchemy event API is untyped
        _conn_rec: Any,  # reason: SQLAlchemy event API is untyped
        cargs: Any,  # reason: SQLAlchemy event API is untyped
        cparams: Any,  # reason: SQLAlchemy event API is untyped
    ) -> Any:  # reason: DBAPI connection
        started = time.perf_counter()
        conn = dialect.connect(*cargs, **cparams)
        record_connect((time.perf_counter() - started) * 1000)
        return conn


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
        engine = create_async_engine(
            url,
            pool_size=POOLED_POOL_SIZE,
            max_overflow=POOLED_MAX_OVERFLOW,
            pool_timeout=POOLED_CHECKOUT_TIMEOUT_S,
            # No pre_ping: it costs a round trip per checkout. A dead connection is
            # caught on the UoW's first statement and retried once (see uow.py).
            pool_recycle=POOLED_RECYCLE_S,
            connect_args=connect_args,
            echo=echo,
        )
        _instrument(engine)
        return engine
    engine = create_async_engine(
        url,
        pool_size=3,
        max_overflow=2,
        pool_pre_ping=True,  # Neon scales to zero; detect connections it dropped
        pool_recycle=240,  # recycle before Neon's 5-minute idle suspend
        connect_args=connect_args,
        echo=echo,
    )
    _instrument(engine)
    return engine


def create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[Any]:  # reason: AsyncSession generic
    return async_sessionmaker(engine, expire_on_commit=False)
