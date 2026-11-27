"""Connected sources (ADR-0033): one sync engine for every OAuth provider that Slate indexes.

Lifecycle:
* **Connect.** IntegrationService calls ``connected`` after a sign-in: the person's private
  source for that connection is created (or found, on a reconnect) and its first sync queued.
* **Sync** (``handle``, worker; ``handle_inline``, "Sync now" for ~90 s). Page by page: get
  a fresh token (refreshing if due), ``connector.pull`` one page, index changed documents
  (unchanged ones are skipped by checksum before any paid call), delete what's gone, save the
  checkpoint. A run stops cleanly and continues later when it is out of time, at its document
  cap, out of budget, or rate-limited.
* **Reconnect needed.** A refused token marks the connection ``needs_reconnect``
  (IntegrationService), the run fails with a plain reason, and hourly syncs skip it. Indexed
  documents stay searchable until the person reconnects or disconnects.
* **Disconnect** (from the source page) revokes and deletes the connection; the source and
  everything indexed from it go with it (FK cascade, migration 0010).

Connected sources are private to the person who connected them (ADR-0026) and are owned
only by them: nobody else can sync or disconnect them.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

import structlog

from core.domain.auth import Identity, Role
from core.domain.connected import (
    MAX_DOCUMENTS_PER_RUN,
    FetchedDocument,
    checkpoint_json,
    document_checksum,
)
from core.domain.data_class import DataClass
from core.domain.documents import DocumentUpsert
from core.domain.errors import (
    AppError,
    BudgetExceeded,
    Conflict,
    NotFound,
    PermissionDenied,
    RateLimited,
    ReconnectRequired,
    UpstreamUnavailable,
)
from core.domain.ids import JobId, RunId, SourceId
from core.domain.integrations import ConnectionStatus, ProviderSpec
from core.domain.jobs import Job
from core.domain.principals import user_principal
from core.domain.skips import SkipReason, Skips
from core.ports.connected import ConnectionAccess, SourceConnector
from core.ports.integrations import Connection, ConnectionStore
from core.ports.jobs import JobQueue
from core.ports.rate_limit import Limit, RateLimiter
from core.ports.sources import SourceRecord, SourceStore
from core.ports.system import Clock
from core.services.ingest import IngestService, Stage
from core.services.sync_run import RunBoundary

log = structlog.get_logger(__name__)

SYNC_CONNECTED = "sync_connected"
INLINE_SYNC_BUDGET_S = 90.0
PROGRESS_EVERY = 10
SYNC_NOW_LIMIT = Limit(budget=1, window=timedelta(minutes=2))
NOT_FOUND = "This source doesn't exist."


@dataclass
class _Tally:
    seen: int = 0
    changed: int = 0
    failed: int = 0
    deleted: int = 0


class _Stop(Exception):
    """Out of time or at the per-run cap: continue in a fresh run."""


@dataclass
class _Run:
    source: SourceRecord
    run: RunId
    existing: dict[str, str]
    tally: _Tally = field(default_factory=_Tally)
    # The last checkpoint committed (after a fully indexed page). Whatever stops the run,
    # this is what's kept, so a stop never rewinds progress (and never skips a page).
    committed: dict[str, object] = field(default_factory=dict[str, object])
    skips: Skips = field(default_factory=Skips)  # the run boundary's tally (P0-4)


def _kind(doc: FetchedDocument) -> str | None:
    kind = doc.metadata.get("kind")
    return kind if isinstance(kind, str) else None


class ConnectedSources:
    def __init__(
        self,
        sources: SourceStore,
        jobs: JobQueue,
        ingest: IngestService | None,
        connections: ConnectionStore,
        access: ConnectionAccess,
        limiter: RateLimiter,
        clock: Clock,
        *,
        connectors: Mapping[str, SourceConnector],
        specs: Mapping[str, ProviderSpec],
    ) -> None:
        self._sources = sources
        self._jobs = jobs
        self._ingest = ingest
        self._connections = connections
        self._access = access
        self._limiter = limiter
        self._clock = clock
        self._connectors = dict(connectors)
        self._specs = dict(specs)

    @property
    def available(self) -> bool:
        """Syncing needs the indexing pipeline (an OpenAI key)."""
        return self._ingest is not None

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(self._connectors)

    # ------------------------------------------------------------ lifecycle

    async def connected(self, connection: Connection) -> SourceId | None:
        """After a sign-in or reconnect: make sure the source exists, queue a sync. Never
        raises: the connection is already saved, and "Sync now" finishes the setup later."""
        if connection.provider not in self._connectors:
            return None
        try:
            source = await self._ensure_source(connection)
            await self._enqueue(source, f"connected:{int(self._clock.now().timestamp())}")
            return source.id
        except AppError as e:
            log.warning("connected.setup_failed", provider=connection.provider, error=e.code)
            return None

    async def source_for(self, identity: Identity, connection_id: UUID) -> SourceId:
        """The source one of your connections syncs into, created if it's missing (a setup
        that failed after sign-in). Callers then sync it like any source: "Sync now" through
        SourceActions runs it inline for ~90 s, then the worker finishes."""
        connection = await self._connections.get(
            identity.workspace_id, identity.user_id, connection_id
        )
        if connection is None or connection.provider not in self._connectors:
            raise NotFound("This connection doesn't exist.")
        return (await self._ensure_source(connection)).id

    async def sync_connection(self, identity: Identity, connection_id: UUID) -> SourceId:
        """Queue a sync for one of your connections (no inline run)."""
        source_id = await self.source_for(identity, connection_id)
        await self.sync_now(identity, source_id)
        return source_id

    async def sync_now(self, identity: Identity, source_id: SourceId) -> JobId:
        """ "Sync now" on the source page (SourceActions)."""
        source = await self._own_source(identity, source_id)
        await self._sync_now_checks(identity, source)
        return await self._enqueue(source, f"manual:{int(self._clock.now().timestamp())}")

    async def disconnect(self, identity: Identity, source_id: SourceId) -> None:
        """Revoke and delete the connection; the source and its documents cascade."""
        source = await self._own_source(identity, source_id)
        if source.connection_id is None:  # can't happen (DB CHECK); delete what's there
            await self._sources.delete_source(identity.workspace_id, source.id)
            return
        await self._access.disconnect(identity, source.connection_id)

    async def enqueue_due(self, limit: int = 50) -> int:
        """Hourly: queue a sync for every due source whose connection still works."""
        hour = int(self._clock.now().timestamp()) // 3600
        queued = 0
        for provider in self._connectors:
            for ws, sid in await self._sources.due_sources(provider, limit):
                source = await self._sources.get_source(ws, sid)
                if source is None or source.connection_id is None:
                    continue
                conn = await self._connections.by_id(source.connection_id)
                if conn is None or conn.status is ConnectionStatus.NEEDS_RECONNECT:
                    continue  # waiting for the person to sign in again: don't hammer
                await self._enqueue(source, f"hourly:{hour}")
                queued += 1
        return queued

    # ------------------------------------------------------------ the job

    async def handle(self, job: Job) -> None:
        await self.sync(job, deadline=None)

    async def handle_inline(self, job: Job) -> None:
        await self.sync(job, deadline=time.monotonic() + INLINE_SYNC_BUDGET_S)

    async def sync(self, job: Job, *, deadline: float | None) -> None:
        source = await self._job_source(job)
        if source is None:
            log.info("connected.sync_skipped", reason="source gone or paused")
            return
        connector = self._connectors[source.connector]
        assert source.connection_id is not None  # _job_source checks it
        cp = dict(source.checkpoint or {})
        kind = "incremental" if cp else "full"
        state = _Run(source, await self._sources.create_run(source.id, kind), {}, committed=cp)
        out_of_time = (lambda: False) if deadline is None else (lambda: time.monotonic() > deadline)
        async with RunBoundary(
            self._sources, source.id, state.run, event="connected.synced",
            counts=lambda: state.tally, checkpoint=lambda: checkpoint_json(state.committed),
            log_fields=lambda: {"provider": source.connector, "deleted": state.tally.deleted},
        ) as boundary:  # fmt: skip
            state.skips = boundary.skips
            try:
                state.existing = await self._sources.document_checksums(source.id, "")
                await self._pages(connector, source.connection_id, state, out_of_time)
            except _Stop:
                boundary.partial("Still syncing: the rest continues in the background.")
                await self._enqueue(source, f"cont:{state.run}")
            except ReconnectRequired:
                name = self._name(source.connector)
                await boundary.fail(f"Sign in to {name} again to keep this source up to date.")

    async def _pages(
        self,
        connector: SourceConnector,
        connection_id: UUID,
        state: _Run,
        out_of_time: Callable[[], bool],
    ) -> None:
        """Pull and index pages until the pass is done, committing each fully indexed page's
        checkpoint (``state.committed``). A page whose items didn't all index (retryably) is
        not committed, so the next run re-reads it."""
        while True:
            if out_of_time() or state.tally.seen >= MAX_DOCUMENTS_PER_RUN:
                raise _Stop
            token = await self._access.access_token(connection_id)
            page = await connector.pull(token, state.committed, now=self._clock.now())
            complete = True
            state.skips.merge(page.skipped)
            for doc in page.documents:
                complete = await self._index(doc, state) and complete
            if page.deleted:
                state.tally.deleted += await self._sources.delete_documents(
                    state.source.id, list(page.deleted)
                )
            if page.deleted_prefixes:
                gone = [e for e in state.existing if e.startswith(page.deleted_prefixes)]
                state.tally.deleted += await self._sources.delete_documents(state.source.id, gone)
                for e in gone:
                    state.existing.pop(e, None)
            if page.keep_only is not None and complete:
                keep = set(page.keep_only)
                gone = [ext for ext in state.existing if ext not in keep]
                state.tally.deleted += await self._sources.delete_documents(state.source.id, gone)
                for ext in gone:
                    state.existing.pop(ext, None)
            if not complete:
                return  # keep the old checkpoint: the next run re-reads this page
            state.committed = checkpoint_json(page.checkpoint)
            await self._sources.save_checkpoint(state.source.id, state.committed, synced=False)
            if page.done:
                return

    async def _index(self, doc: FetchedDocument, state: _Run) -> bool:
        """True when indexed, unchanged, or failed for good (recorded). False when it failed
        in a way worth retrying. BudgetExceeded propagates (the run stops, resumable)."""
        assert self._ingest is not None  # _job_source checks availability
        t = state.tally
        if not doc.sections:
            # Counted, not silently dropped (P0-4); not "seen", as nothing was indexable.
            state.skips.add(SkipReason.EMPTY_TEXT, _kind(doc))
            return True
        t.seen += 1
        if t.seen % PROGRESS_EVERY == 0:
            await self._sources.progress(state.run, seen=t.seen, changed=t.changed, failed=t.failed)
        digest = document_checksum(doc)
        if state.existing.get(doc.external_id) == digest:
            return True
        source, stage = state.source, Stage("chunk")
        try:
            result = await self._ingest.index_document(
                source.workspace_id,
                DocumentUpsert(
                    source_id=source.id,
                    external_id=doc.external_id,
                    uri=doc.uri,
                    title=doc.title,
                    mime=doc.mime,
                    checksum=digest,
                    acl=source.visibility,
                    author=doc.author,
                    source_created_at=doc.created_at,
                    source_updated_at=doc.updated_at,
                    metadata=dict(doc.metadata),
                ),
                doc.sections,
                data_class=source.data_class,
                stage=stage,
            )
        except BudgetExceeded:
            raise
        except AppError as e:
            t.failed += 1
            await self._sources.record_error(
                state.run, external_id=doc.external_id, stage=stage.name, message=e.detail,
                retryable=e.retryable,
            )  # fmt: skip
            return not e.retryable
        state.existing[doc.external_id] = digest
        if result.changed:
            t.changed += 1
        return True

    # ------------------------------------------------------------ helpers

    async def _ensure_source(self, connection: Connection) -> SourceRecord:
        ws = connection.workspace_id
        for s in await self._sources.sources_by_connector(ws, connection.provider):
            if s.connection_id == connection.id:
                return s
        label = connection.account_label or connection.account_id
        base = f"{self._name(connection.provider)} · {label}"[:200]
        kwargs: dict[str, object] = {
            "connector": connection.provider,
            "config": {"account": label, "connection_id": str(connection.id)},
            "visibility": [user_principal(connection.user_id)],
            "data_class": DataClass.PRIVATE,
            "created_by": connection.user_id,
            "connection_id": connection.id,
        }
        try:
            sid = await self._sources.create_source(ws, name=base, **kwargs)  # type: ignore[arg-type]
        except Conflict:  # another member's source has that name (a shared Slack team)
            name = f"{base} ({str(connection.id)[:6]})"
            sid = await self._sources.create_source(ws, name=name, **kwargs)  # type: ignore[arg-type]
        source = await self._sources.get_source(ws, sid)
        if source is None:
            raise NotFound(NOT_FOUND)
        log.info("connected.source_created", provider=connection.provider)
        return source

    async def _own_source(self, identity: Identity, source_id: SourceId) -> SourceRecord:
        source = await self._sources.get_source(identity.workspace_id, source_id)
        if (
            source is None
            or source.connector not in self._connectors
            or source.created_by != identity.user_id
        ):
            raise NotFound(NOT_FOUND)  # someone else's: the same 404 (ADR-0026)
        return source

    async def _sync_now_checks(self, identity: Identity, source: SourceRecord) -> None:
        if identity.role is Role.VIEWER:
            raise PermissionDenied("Viewers can't sync sources.")
        if not self.available:
            raise UpstreamUnavailable("Syncing isn't set up on this server yet.")
        if not await self._limiter.try_spend(f"sync-now:{source.id}", 1, SYNC_NOW_LIMIT):
            raise RateLimited(
                "This source synced a moment ago. Try again shortly.", retry_after_s=120
            )

    async def _enqueue(self, source: SourceRecord, suffix: str) -> JobId:
        return await self._jobs.enqueue(
            source.workspace_id, SYNC_CONNECTED, {"source_id": str(source.id)},
            f"{SYNC_CONNECTED}:{source.id}:{suffix}",
        )  # fmt: skip

    async def _job_source(self, job: Job) -> SourceRecord | None:
        try:
            sid = SourceId(UUID(str(job.payload.get("source_id"))))
        except ValueError:
            return None
        source = await self._sources.get_source(job.workspace_id, sid)
        if (
            source is None
            or source.status != "active"
            or source.connector not in self._connectors
            or source.connection_id is None
            or not self.available
        ):
            return None
        return source

    def _name(self, provider: str) -> str:
        spec = self._specs.get(provider)
        return spec.name if spec else provider
