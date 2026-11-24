"""Per-request counters shared by entry points and adapters (stdlib only).

A ``ContextVar`` is a "global" that is private to each asyncio task, so concurrent
requests each see their own counters. The API middleware starts a fresh set per
request; the Postgres engine increments it whenever it opens a physical connection.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(slots=True)
class ConnectStats:
    """New physical DB connections opened while serving one request."""

    count: int = 0
    total_ms: float = 0.0


_connect_stats: ContextVar[ConnectStats | None] = ContextVar("db_connect_stats", default=None)


def start_connect_stats() -> ConnectStats:
    """Begin counting for the current request; returns the live counters."""
    stats = ConnectStats()
    _connect_stats.set(stats)
    return stats


def record_connect(duration_ms: float) -> None:
    """Called by the DB adapter after each new connection. No-op outside a request."""
    stats = _connect_stats.get()
    if stats is not None:
        stats.count += 1
        stats.total_ms += duration_ms
