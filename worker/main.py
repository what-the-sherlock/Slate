"""Worker composition root and CLI: the only worker/ module that constructs adapters.

    uv run python -m worker run              drain runnable jobs, then exit
    uv run python -m worker run --once       process at most one job
    uv run python -m worker run --max-seconds 600

GitHub Actions (sync.yml) runs ``run`` on repository_dispatch and hourly. The process
exits when the queue is empty, so the database can scale back to zero.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import socket
import uuid

import structlog

from core.adapters.postgres.engine import (
    EngineKind,
    create_engine,
    create_session_factory,
    use_selector_loop_on_windows,
)
from core.adapters.postgres.jobs import PostgresJobQueue
from core.adapters.postgres.uow import make_uow
from core.config import get_settings
from core.logs import configure_logging
from worker.handlers import NOOP, noop
from worker.runner import Handler, RunLimits, RunSummary, Worker

log = structlog.get_logger("worker")

HANDLERS: dict[str, Handler] = {NOOP: noop}


def _worker_id(configured: str | None) -> str:
    return configured or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def _install_signal_handlers(worker: Worker) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.request_stop)
        except (NotImplementedError, RuntimeError):  # Windows: no loop signal handlers
            signal.signal(sig, lambda *_: worker.request_stop())


async def run(limits: RunLimits) -> RunSummary:
    settings = get_settings()
    engine = create_engine(settings.require_database_url(direct=True), EngineKind.DIRECT)
    try:
        queue = PostgresJobQueue(make_uow(create_session_factory(engine)))
        worker = Worker(queue, HANDLERS, _worker_id(settings.worker_id))
        _install_signal_handlers(worker)
        return await worker.run(limits)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m worker")
    sub = parser.add_subparsers(dest="command", required=True)
    run_cmd = sub.add_parser("run", help="process runnable jobs, then exit")
    run_cmd.add_argument("--once", action="store_true", help="process at most one job")
    run_cmd.add_argument("--max-jobs", type=int, default=None)
    run_cmd.add_argument("--max-seconds", type=float, default=RunLimits().max_seconds)
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, json=settings.env != "dev")
    limits = RunLimits(max_jobs=1 if args.once else args.max_jobs, max_seconds=args.max_seconds)
    use_selector_loop_on_windows()
    asyncio.run(run(limits))
    return 0  # job failures are recorded in the DB; a non-zero exit means the worker crashed
