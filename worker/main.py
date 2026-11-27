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
from datetime import UTC, datetime
from decimal import Decimal

import structlog

from core.adapters.factories import (
    connected_sources,
    credential_store,
    discord_service,
    discord_sync,
    github_service,
    github_sync,
    ingest_service,
    integration_service,
    mail_service,
    mail_sync,
    mbox_import,
    s3_blob_store,
    workersai_embedder,
)
from core.adapters.postgres.auth import PostgresAccountStore
from core.adapters.postgres.documents import PostgresDocumentStore
from core.adapters.postgres.engine import (
    EngineKind,
    create_engine,
    create_session_factory,
    use_selector_loop_on_windows,
)
from core.adapters.postgres.jobs import PostgresJobQueue
from core.adapters.postgres.sources import PostgresSourceStore
from core.adapters.postgres.uow import UnitOfWork, make_uow
from core.adapters.postgres.usage import PostgresGenerationStore, PostgresUsageLedger
from core.config import Settings, get_settings
from core.domain.github import CREDENTIAL_KIND as GITHUB_TOKEN
from core.logs import configure_logging
from core.services.backfill import BackfillReport, Provider, backfill_generation
from core.services.connected import SYNC_CONNECTED
from core.services.discord import SYNC_DISCORD
from core.services.github import SYNC_GITHUB
from core.services.mail import IMPORT_MBOX, SYNC_MAIL
from core.services.maintenance import sweep_abandoned_uploads, sweep_unverified_accounts
from core.services.uploads import PROCESS_UPLOAD
from worker.handlers import NOOP, noop
from worker.runner import Handler, RunLimits, RunSummary, Worker

log = structlog.get_logger("worker")


def build_handlers(settings: Settings, uow: UnitOfWork) -> dict[str, Handler]:
    handlers: dict[str, Handler] = {NOOP: noop}
    ingest = ingest_service(settings, uow)
    if ingest is not None:
        handlers[PROCESS_UPLOAD] = ingest.process_upload
    else:
        # Jobs of that kind simply wait in the queue until the config is fixed.
        log.warning("upload_pipeline_disabled", reason="storage or OPENAI_API_KEY not configured")
    sync = github_sync(settings, uow)
    if sync is not None:
        handlers[SYNC_GITHUB] = sync.handle
    else:
        log.warning("github_sync_disabled", reason="GitHub App or OPENAI_API_KEY not configured")
    dsync = discord_sync(settings, uow)
    if dsync is not None:
        handlers[SYNC_DISCORD] = dsync.handle
    else:
        log.warning("discord_sync_disabled", reason="bot token or OPENAI_API_KEY not configured")
    msync = mail_sync(settings, uow)
    if msync is not None:
        handlers[SYNC_MAIL] = msync.handle
    else:
        log.warning("mail_sync_disabled", reason="CREDENTIALS_FERNET_KEY or OPENAI_API_KEY missing")
    connected = connected_sources(settings, uow, integration_service(settings, uow, {}))
    if connected.available:
        handlers[SYNC_CONNECTED] = connected.handle
    else:
        log.warning("connected_sync_disabled", reason="OPENAI_API_KEY not configured")
    mbox = mbox_import(settings, uow)
    if mbox is not None:
        handlers[IMPORT_MBOX] = mbox.handle
    return handlers


async def housekeeping(settings: Settings, uow: UnitOfWork) -> None:
    # Housekeeping must never stop real work, so each sweep fails on its own.
    try:
        await sweep_unverified_accounts(PostgresAccountStore(uow), datetime.now(UTC))
    except Exception as e:
        log.warning("sweep_failed", sweep="unverified_accounts", error_type=type(e).__name__)
    try:
        # Hourly: queue a sync for every repo whose interval has passed (ADR-0023).
        queued = await github_service(settings, uow).enqueue_due()
        if queued:
            log.info("github_syncs_queued", count=queued)
    except Exception as e:
        log.warning("sweep_failed", sweep="github_schedule", error_type=type(e).__name__)
    try:
        queued = await discord_service(settings, uow).enqueue_due()
        if queued:
            log.info("discord_syncs_queued", count=queued)
    except Exception as e:
        log.warning("sweep_failed", sweep="discord_schedule", error_type=type(e).__name__)
    try:
        # Tokens kept from a GitHub connect that was never finished, or replaced by a reconnect.
        swept = await credential_store(settings, uow).sweep_unreferenced(GITHUB_TOKEN, 24)
        if swept:
            log.info("credentials_swept", count=swept)
    except Exception as e:
        log.warning("sweep_failed", sweep="credentials", error_type=type(e).__name__)
    try:
        queued = await mail_service(settings, uow).enqueue_due()
        if queued:
            log.info("mail_syncs_queued", count=queued)
    except Exception as e:
        log.warning("sweep_failed", sweep="mail_schedule", error_type=type(e).__name__)
    try:
        # Hourly: Gmail, Notion, Linear, Calendar, Drive, Slack (ADR-0033). Connections
        # waiting for a reconnect are skipped.
        engine = connected_sources(settings, uow, integration_service(settings, uow, {}))
        queued = await engine.enqueue_due()
        if queued:
            log.info("connected_syncs_queued", count=queued)
    except Exception as e:
        log.warning("sweep_failed", sweep="connected_schedule", error_type=type(e).__name__)
    blobs = s3_blob_store(settings)
    if blobs is None:
        return
    try:
        await sweep_abandoned_uploads(blobs, PostgresSourceStore(uow))
    except Exception as e:
        log.warning("sweep_failed", sweep="abandoned_uploads", error_type=type(e).__name__)


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
        uow = make_uow(create_session_factory(engine))
        await housekeeping(settings, uow)
        queue = PostgresJobQueue(uow)
        worker = Worker(queue, build_handlers(settings, uow), _worker_id(settings.worker_id))
        _install_signal_handlers(worker)
        return await worker.run(limits)
    finally:
        await engine.dispose()


async def backfill(max_chunks: int) -> BackfillReport:
    """Build the BGE-M3 'future' generation from public chunks (ADR-0004, ADR-0012)."""
    settings = get_settings()
    embedder = workersai_embedder(settings)
    if embedder is None:
        raise SystemExit("backfill needs CF_ACCOUNT_ID and CF_API_TOKEN in the environment")
    engine = create_engine(settings.require_database_url(direct=True), EngineKind.DIRECT)
    try:
        uow = make_uow(create_session_factory(engine))
        return await backfill_generation(
            PostgresDocumentStore(uow),
            PostgresGenerationStore(uow),
            PostgresUsageLedger(uow),
            embedder,
            Provider("workersai", free_tier=True),
            data_scope="public",
            monthly_budget_usd=Decimal(str(settings.monthly_llm_budget_usd)),
            max_chunks=max_chunks,
        )
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m worker")
    sub = parser.add_subparsers(dest="command", required=True)
    run_cmd = sub.add_parser("run", help="process runnable jobs, then exit")
    run_cmd.add_argument("--once", action="store_true", help="process at most one job")
    run_cmd.add_argument("--max-jobs", type=int, default=None)
    run_cmd.add_argument("--max-seconds", type=float, default=RunLimits().max_seconds)
    bf = sub.add_parser("backfill", help="embed public chunks with BGE-M3 (Workers AI)")
    bf.add_argument("--max-chunks", type=int, default=2000, help="stop after this many")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, json=settings.env != "dev")
    use_selector_loop_on_windows()
    if args.command == "backfill":
        report = asyncio.run(backfill(args.max_chunks))
        state = "complete" if report.complete else "paused (run again to continue)"
        print(f"generation {int(report.generation)}: embedded {report.embedded} chunks, {state}")
        return 0
    limits = RunLimits(max_jobs=1 if args.once else args.max_jobs, max_seconds=args.max_seconds)
    asyncio.run(run(limits))
    return 0  # job failures are recorded in the DB; a non-zero exit means the worker crashed
