"""Email sources (ADR-0028): a live IMAP mailbox, or a one-off .mbox import.

Connect (``MailService.connect``): the user picks a provider from a fixed list, types their
address and an *app password*. Slate logs in once to check it (read-only), then stores the
password Fernet-encrypted in ``credentials``. The source is private data readable only by
that user (ADR-0026; email is always private, a DB CHECK). Login attempts are rate-limited
per user, so Slate can't be used to guess someone's app password.

Sync (``MailSync``): Inbox and Sent are read read-only. The first sync reads the newest
messages of the last 90 days and keeps the 2,000 most recent conversations; later syncs read
only UIDs after the saved cursor, then fetch the rest of each touched conversation, so a
thread document is always complete. A thread is one document (``core.domain.mail``).

Import (``MboxImport``): a Google Takeout .mbox (25 MB, uploaded straight to storage like any
upload) is split into the same thread documents in the user's "Email import" source. The
file is deleted once imported.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from uuid import UUID

import structlog

from core.domain.attachments import blob_digest
from core.domain.auth import Identity, Role
from core.domain.data_class import DataClass
from core.domain.documents import DocumentUpsert, Section
from core.domain.errors import (
    AppError,
    BudgetExceeded,
    Conflict,
    NotFound,
    PermissionDenied,
    RateLimited,
    UpstreamUnavailable,
    ValidationFailed,
)
from core.domain.ids import JobId, RunId, SourceId, WorkspaceId
from core.domain.jobs import Job, JobStatus
from core.domain.mail import (
    MAX_BACKFILL_THREADS,
    MAX_MESSAGES_PER_FOLDER_PER_SYNC,
    MAX_THREAD_MESSAGES,
    MAX_THREADS_PER_SYNC,
    PROVIDERS,
    FolderCursor,
    FolderRole,
    Mail,
    MailCheckpoint,
    Provider,
    backfill_since,
    cap_attachment_bytes,
    checksum,
    group_threads,
    is_plausible_app_password,
    is_valid_address,
    newest_threads,
    normalise_app_password,
    participants,
    thread_external_id,
    thread_files,
    thread_metadata,
    thread_sections,
    thread_title,
)
from core.domain.principals import user_principal
from core.ports.credentials import CredentialStore
from core.ports.jobs import JobQueue
from core.ports.mailbox import Mailbox, MailSession
from core.ports.rate_limit import Limit, RateLimiter
from core.ports.sources import SourceRecord, SourceStore
from core.ports.storage import BlobStore
from core.ports.system import Clock
from core.services.attachments import AttachmentIndexer, ParentRef
from core.services.ingest import IngestService, Stage
from core.services.sync_run import RunBoundary

log = structlog.get_logger(__name__)

IMAP = "imap"
MBOX = "gmail_mbox"
SYNC_MAIL = "sync_mail"
IMPORT_MBOX = "import_mbox"
CREDENTIAL_KIND = "imap_app_password"
INLINE_SYNC_BUDGET_S = 90.0
QUICK_SYNC_BUDGET_S = 15.0  # a sync a question waits for (ADR-0043)
PROGRESS_EVERY = 10  # items between live progress updates on a running run
LOGIN_LIMIT = Limit(budget=5, window=timedelta(hours=1))
LOGIN_DAY_LIMIT = Limit(budget=20, window=timedelta(days=1))
SYNC_NOW_LIMIT = Limit(budget=1, window=timedelta(minutes=5))
NOT_SET_UP = "Email isn't set up on this server yet."
RECONNECT = "Slate can't use the saved app password any more. Disconnect and connect again."
MIME = "message/rfc822"


@dataclass(frozen=True, slots=True)
class MailConnected:
    source_id: SourceId
    job_id: JobId
    name: str


class MailService:
    def __init__(
        self,
        mailbox: Mailbox,
        credentials: CredentialStore,
        sources: SourceStore,
        jobs: JobQueue,
        limiter: RateLimiter,
        clock: Clock,
    ) -> None:
        self._mailbox = mailbox
        self._credentials = credentials
        self._sources = sources
        self._jobs = jobs
        self._limiter = limiter
        self._clock = clock

    @property
    def available(self) -> bool:
        return self._credentials.available

    async def connect(
        self, identity: Identity, *, provider: Provider, address: str, password: str
    ) -> MailConnected:
        if not self.available:
            raise UpstreamUnavailable(NOT_SET_UP)
        if not identity.can(Role.MEMBER):
            raise PermissionDenied("Your role can't connect sources.")
        address = address.strip()
        password = normalise_app_password(password)
        if not is_valid_address(address):
            raise ValidationFailed("That isn't a valid email address.")
        if not is_plausible_app_password(password):
            raise ValidationFailed("Paste the app password your provider generated.")
        ws = identity.workspace_id
        if any(
            str(s.config.get("address", "")).lower() == address.lower()
            for s in await self._sources.sources_by_connector(ws, IMAP)
        ):
            raise Conflict("This mailbox is already connected.")
        # Every attempt counts (before the login), so Slate can't be used to guess passwords.
        key = f"mail-login:{identity.user_id}"
        if not await self._limiter.try_spend(key, 1, LOGIN_LIMIT) or not (
            await self._limiter.try_spend(key + ":day", 1, LOGIN_DAY_LIMIT)
        ):
            raise RateLimited(
                "Too many sign-in attempts. Try again in an hour.", retry_after_s=3600
            )
        async with self._mailbox.session(provider, address, password) as session:
            folders = await session.folders()
        cred = await self._credentials.save(
            ws, kind=CREDENTIAL_KIND, secret=password, created_by=identity.user_id
        )
        name = f"{PROVIDERS[provider].name} · {address}"
        try:
            sid = await self._sources.create_source(
                ws,
                connector=IMAP,
                name=name,
                config={
                    "provider": provider.value,
                    "address": address,
                    "folders": [{"name": f.name, "role": f.role.value} for f in folders],
                },
                visibility=[user_principal(identity.user_id)],
                data_class=DataClass.PRIVATE,
                created_by=identity.user_id,
                credential_id=cred,
            )
        except BaseException:
            await self._credentials.delete(ws, cred)  # nothing orphaned, whatever failed
            raise
        job = await self._jobs.enqueue(
            ws, SYNC_MAIL, {"source_id": str(sid)}, f"{SYNC_MAIL}:{sid}:initial"
        )
        log.info("mail.connected", provider=provider.value, folders=len(folders))
        return MailConnected(sid, job, name)

    async def sync_now(self, identity: Identity, source_id: SourceId) -> JobId:
        source = await self._source(identity, source_id)
        initial = await self._jobs.find_by_key(f"{SYNC_MAIL}:{source.id}:initial")
        if initial is not None and initial.status in (JobStatus.QUEUED, JobStatus.FAILED):
            return initial.id
        if not await self._limiter.try_spend(f"mail-sync:{source.id}", 1, SYNC_NOW_LIMIT):
            raise RateLimited(
                "This mailbox was synced a moment ago. Try again in a few minutes.",
                retry_after_s=300,
            )
        bucket = int(self._clock.now().timestamp()) // 300
        return await self._jobs.enqueue(
            identity.workspace_id, SYNC_MAIL, {"source_id": str(source.id)},
            f"{SYNC_MAIL}:{source.id}:manual:{bucket}",
        )  # fmt: skip

    async def disconnect(self, identity: Identity, source_id: SourceId) -> None:
        """Delete the source, everything indexed from it, and its encrypted password."""
        source = await self._source(identity, source_id, any_email=True)
        if not identity.can(Role.ADMIN) and source.created_by != identity.user_id:
            raise PermissionDenied("Only admins, the owner and its connector can disconnect it.")
        if not await self._sources.delete_source(identity.workspace_id, source.id):
            raise NotFound("This source doesn't exist.")
        if source.credential_id is not None:  # the DB trigger does it too (migration 0007)
            await self._credentials.delete(identity.workspace_id, source.credential_id)
        log.info("mail.disconnected")

    async def enqueue_due(self, limit: int = 50) -> int:
        hour = int(self._clock.now().timestamp()) // 3600
        due = await self._sources.due_sources(IMAP, limit)
        for ws, sid in due:
            await self._jobs.enqueue(
                ws, SYNC_MAIL, {"source_id": str(sid)}, f"{SYNC_MAIL}:{sid}:{hour}"
            )
        return len(due)

    async def _source(
        self, identity: Identity, source_id: SourceId, *, any_email: bool = False
    ) -> SourceRecord:
        source = await self._sources.get_source(identity.workspace_id, source_id)
        allowed = {IMAP, MBOX} if any_email else {IMAP}
        if source is None or source.connector not in allowed:
            raise NotFound("This source doesn't exist.")
        return source


class MboxActions:
    """Sync now / disconnect for an .mbox import source: nothing to sync; disconnect deletes."""

    def __init__(self, service: MailService) -> None:
        self._svc = service

    @property
    def available(self) -> bool:
        return True

    async def sync_now(self, identity: Identity, source_id: SourceId) -> JobId:
        raise ValidationFailed("An imported mailbox doesn't sync. Import a newer export instead.")

    async def disconnect(self, identity: Identity, source_id: SourceId) -> None:
        await self._svc.disconnect(identity, source_id)


# ====================================================================== indexing


@dataclass
class _Tally:
    seen: int = 0
    changed: int = 0
    failed: int = 0


@dataclass
class _Indexer:
    """Indexes whole threads; shared by the live sync and the .mbox import."""

    ingest: IngestService
    sources: SourceStore
    source: SourceRecord
    run: RunId
    existing: Mapping[str, str]
    tally: _Tally = field(default_factory=_Tally)
    # ADR-0043: reads the files that came with each conversation (None: listed only),
    # and which folders hold the account's own sent mail.
    attachments: AttachmentIndexer | None = None
    sent_folders: frozenset[str] = frozenset()

    async def thread(self, key: str, mails: Sequence[Mail]) -> bool:
        """True when indexed, unchanged, or failed permanently (recorded: retrying would fail
        the same way, so it must not hold the cursor and make every sync re-read). False when
        it failed in a way worth retrying. BudgetExceeded propagates."""
        ext = thread_external_id(key)
        if self.sent_folders:
            mails = [replace(m, sent=True) if m.folder in self.sent_folders else m for m in mails]
        sections: tuple[Section, ...] = thread_sections(mails)
        title = thread_title(mails)
        self.tally.seen += 1
        if self.tally.seen % PROGRESS_EVERY == 0:
            t = self.tally
            await self.sources.progress(self.run, seen=t.seen, changed=t.changed, failed=t.failed)
        files, blobs = thread_files(mails)
        digest = checksum(sections, title)
        if blobs:  # a file replaced under the same name and size still counts
            digest = hashlib.sha256((digest + blob_digest(blobs)).encode()).hexdigest()
        if not sections or self.existing.get(ext) == digest:
            return True
        ordered = sorted(mails, key=lambda m: m.date)
        metadata = thread_metadata(mails)
        prepared = await self.attachments.prepare(files, blobs) if self.attachments else []
        if prepared:
            metadata["attachments"] = [p.info.to_meta() for p in prepared]
        stage = Stage("chunk")
        try:
            result = await self.ingest.index_document(
                self.source.workspace_id,
                DocumentUpsert(
                    source_id=self.source.id,
                    external_id=ext,
                    uri=f"mail:{ext}",  # no public link: previews go through Slate
                    title=title,
                    mime=MIME,
                    checksum=digest,
                    acl=self.source.visibility,
                    author=participants(ordered),
                    source_created_at=ordered[0].date,
                    source_updated_at=ordered[-1].date,
                    metadata=metadata,
                ),
                sections,
                data_class=self.source.data_class,
                stage=stage,
            )
        except BudgetExceeded:
            raise
        except AppError as e:
            self.tally.failed += 1
            await self.sources.record_error(
                self.run, external_id=ext, stage=stage.name, message=e.detail, retryable=e.retryable
            )
            return not e.retryable
        if result.changed:
            self.tally.changed += 1
            if self.attachments is not None:  # also prunes files no longer in the thread
                await self.attachments.write(
                    ParentRef(
                        workspace_id=self.source.workspace_id, source_id=self.source.id,
                        external_id=ext, title=title, uri=f"mail:{ext}",
                        acl=self.source.visibility, data_class=self.source.data_class,
                        author=participants(ordered), created_at=ordered[0].date,
                        updated_at=ordered[-1].date,
                    ),
                    prepared,
                )  # fmt: skip
        return True


class _OutOfTime(Exception):
    pass


class MailSync:
    """The ``sync_mail`` job handler (worker and inline)."""

    def __init__(
        self,
        mailbox: Mailbox,
        credentials: CredentialStore,
        sources: SourceStore,
        jobs: JobQueue,
        ingest: IngestService,
        clock: Clock,
        attachments: AttachmentIndexer | None = None,
    ) -> None:
        self._attachments = attachments
        self._mailbox = mailbox
        self._credentials = credentials
        self._sources = sources
        self._jobs = jobs
        self._ingest = ingest
        self._clock = clock

    async def handle(self, job: Job) -> None:
        await self.sync(job, deadline=None)

    async def handle_inline(self, job: Job) -> None:
        await self.sync(job, deadline=time.monotonic() + INLINE_SYNC_BUDGET_S)

    async def handle_quick(self, job: Job) -> None:
        """A sync a question started (ADR-0043): a short, cooperative budget; whatever is
        left continues in the worker from the saved checkpoint."""
        await self.sync(job, deadline=time.monotonic() + QUICK_SYNC_BUDGET_S)

    async def sync(self, job: Job, *, deadline: float | None) -> None:
        source = await self._sources.get_source(job.workspace_id, _source_id(job))
        if source is None or source.connector != IMAP or source.status != "active":
            log.info("mail.sync_skipped", reason="source gone or paused")
            return
        cp = MailCheckpoint.from_json(source.checkpoint)
        run = await self._sources.create_run(source.id, "full" if cp.first_sync else "incremental")
        out_of_time = (lambda: False) if deadline is None else (lambda: time.monotonic() > deadline)
        indexer = _Indexer(
            self._ingest, self._sources, source, run, {},
            attachments=self._attachments, sent_folders=_sent_folders(source),
        )  # fmt: skip
        async with RunBoundary(
            self._sources, source.id, run, event="mail.synced",
            counts=lambda: indexer.tally, checkpoint=lambda: cp.as_json(),
            failed_noun="conversation(s)",
        ) as boundary:  # fmt: skip
            try:
                secret = (
                    await self._credentials.secret(source.workspace_id, source.credential_id)
                    if source.credential_id is not None
                    else None
                )
                if secret is None:
                    raise PermissionDenied(RECONNECT)
                try:
                    provider = Provider(str(source.config.get("provider")))
                except ValueError:  # unknown provider in a stored config
                    raise PermissionDenied(RECONNECT) from None
                address = str(source.config.get("address", ""))
                indexer.existing = await self._sources.document_checksums(source.id, "thread:")
                async with self._mailbox.session(provider, address, secret) as session:
                    cp = await self._sync(session, source, cp, indexer, out_of_time)
            except _OutOfTime:
                boundary.partial("Still syncing: the rest continues in the background.")
                await self._jobs.enqueue(
                    source.workspace_id, SYNC_MAIL, {"source_id": str(source.id)},
                    f"{SYNC_MAIL}:{source.id}:cont:{run}",
                )  # fmt: skip

    async def _sync(
        self,
        session: MailSession,
        source: SourceRecord,
        cp: MailCheckpoint,
        indexer: _Indexer,
        out_of_time: Callable[[], bool],
    ) -> MailCheckpoint:
        since = backfill_since(self._clock.now())
        fetched: list[Mail] = []
        incremental: set[str] = set()  # folders read from a valid cursor
        heads: dict[str, FolderCursor] = {}
        for folder in _folders(source):
            if out_of_time():
                raise _OutOfTime
            uv = await session.open_folder(folder)
            cursor = cp.cursor(folder)
            if cursor.valid_for(uv):
                # At most MAX_THREADS_PER_SYNC new messages, so no conversation is dropped by
                # the thread cap below; the rest follows next sync from the cursor.
                uids = (await session.uids_after(cursor.last_uid))[:MAX_THREADS_PER_SYNC]
                incremental.add(folder)
            else:  # first sync, or the server rebuilt the folder (UIDVALIDITY changed)
                uids = (await session.uids_since(since))[-MAX_MESSAGES_PER_FOLDER_PER_SYNC:]
            fetched += await self._fetch(session, uids, out_of_time)
            heads[folder] = FolderCursor(
                uv, max([cursor.last_uid if cursor.valid_for(uv) else 0, *uids])
            )
        threads = group_threads(fetched)
        if incremental and threads:
            threads = await self._complete(session, source, threads, out_of_time)
        first = not incremental
        keys = newest_threads(threads, MAX_BACKFILL_THREADS if first else MAX_THREADS_PER_SYNC)
        ok = True
        for key in reversed(keys):  # oldest of the kept ones first: a stop keeps the order
            if out_of_time():
                raise _OutOfTime
            ok = await indexer.thread(key, threads[key]) and ok
        if not ok:
            return cp  # keep the cursors: the next sync retries (unchanged ones are free)
        for folder, head in heads.items():
            cp = cp.with_cursor(folder, head)
        return cp

    async def _fetch(
        self, session: MailSession, uids: Sequence[int], out_of_time: Callable[[], bool]
    ) -> list[Mail]:
        out: list[Mail] = []
        for i in range(0, len(uids), 200):
            if out_of_time():
                raise _OutOfTime
            out += await session.fetch(uids[i : i + 200])
        return cap_attachment_bytes(out)

    async def _complete(
        self,
        session: MailSession,
        source: SourceRecord,
        threads: dict[str, list[Mail]],
        out_of_time: Callable[[], bool],
    ) -> dict[str, list[Mail]]:
        """New messages joined existing conversations: read the rest of each, in every
        folder, so the rebuilt document holds the whole thread."""
        have = {(m.folder, m.uid) for ms in threads.values() for m in ms}
        merged = {k: list(v) for k, v in threads.items()}
        for folder in _folders(source):
            await session.open_folder(folder)
            for key, mails in list(merged.items())[:MAX_THREADS_PER_SYNC]:
                if out_of_time():
                    raise _OutOfTime
                uids = [u for u in await session.thread_uids(mails[0]) if (folder, u) not in have]
                if uids:
                    extra = await session.fetch(uids[-MAX_THREAD_MESSAGES:])
                    merged[key] = [*merged[key], *extra]
                    have.update((m.folder, m.uid) for m in extra)
        return group_threads(m for ms in merged.values() for m in ms)


def _sent_folders(source: SourceRecord) -> frozenset[str]:
    """The folders the account sends from (role chosen at connect)."""
    raw = source.config.get("folders")
    out: set[str] = set()
    for f in raw if isinstance(raw, list) else []:  # pyright: ignore[reportUnknownVariableType]
        if isinstance(f, dict) and f.get("role") == FolderRole.SENT.value:  # pyright: ignore[reportUnknownMemberType]
            name = f.get("name")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if isinstance(name, str):
                out.add(name)
    return frozenset(out)


def _folders(source: SourceRecord) -> list[str]:
    raw = source.config.get("folders")
    out: list[str] = []
    for f in raw if isinstance(raw, list) else []:  # pyright: ignore[reportUnknownVariableType]
        name = f.get("name") if isinstance(f, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if isinstance(name, str) and 0 < len(name) <= 200:
            out.append(name)
    return out[:4] or ["INBOX"]


def _source_id(job: Job) -> SourceId:
    try:
        return SourceId(UUID(str(job.payload.get("source_id"))))
    except ValueError:
        return SourceId(UUID(int=0))


# ====================================================================== .mbox import

MAX_MBOX_BYTES = 25 * 1024 * 1024


class MboxImport:
    """The ``import_mbox`` job handler: one uploaded export -> thread documents."""

    def __init__(
        self,
        blobs: BlobStore,
        sources: SourceStore,
        ingest: IngestService,
        parse: Callable[[bytes], list[Mail]],
        attachments: AttachmentIndexer | None = None,
    ) -> None:
        self._attachments = attachments
        self._blobs = blobs
        self._sources = sources
        self._ingest = ingest
        self._parse = parse

    async def handle(self, job: Job) -> None:
        p = job.payload
        key, sha = str(p.get("blob_key", "")), str(p.get("sha256", ""))
        uploader = str(p.get("uploader", ""))
        source = await self._sources.get_source(job.workspace_id, _source_id(job))
        if (
            source is None
            or source.connector != MBOX
            or not key.startswith(f"uploads/{job.workspace_id}/{uploader}/")
            or source.visibility != (f"user:{uploader}",)
        ):
            raise ValidationFailed("Malformed job payload.")
        run = await self._sources.create_run(source.id, "import")
        indexer = _Indexer(
            self._ingest, self._sources, source, run, {}, attachments=self._attachments
        )
        async with RunBoundary(
            self._sources, source.id, run, event="mail.imported",
            counts=lambda: indexer.tally, checkpoint=None, failed_noun="conversation(s)",
        ) as boundary:  # fmt: skip
            try:
                size = await self._blobs.size_of(key)
                if size is None:
                    raise ValidationFailed("The uploaded file is missing from storage.")
                if size > MAX_MBOX_BYTES:  # byte cap before downloading anything
                    raise ValidationFailed("The stored file is larger than 25 MB.")
                data = await self._blobs.get(key)
                if len(data) > MAX_MBOX_BYTES:
                    raise ValidationFailed("The stored file is larger than 25 MB.")
                if hashlib.sha256(data).hexdigest() != sha:
                    raise ValidationFailed("The uploaded file changed after it was checked.")
                mails = cap_attachment_bytes(self._parse(data))
                if not mails:
                    raise ValidationFailed("No emails found in this file. Export Mail as .mbox.")
                threads = group_threads(mails)
                indexer.existing = await self._sources.document_checksums(source.id, "thread:")
                for k in reversed(newest_threads(threads, MAX_BACKFILL_THREADS)):
                    await indexer.thread(k, threads[k])
            except BudgetExceeded as e:
                boundary.partial(e.detail + " Import the file again later to finish.")
            except AppError as e:
                await boundary.fail(e.detail, stage="parse")
        status = boundary.status
        if status != "partial":
            await self._blobs.delete(key)  # raw mail isn't kept once it's indexed (or rejected)


async def mbox_source(sources: SourceStore, identity: Identity) -> SourceId:
    """The user's own "Email import" source (created on first use), private to them."""
    ws: WorkspaceId = identity.workspace_id
    name = f"Email import · {identity.name or identity.email}"[:120]
    for s in await sources.sources_by_connector(ws, MBOX):
        if s.created_by == identity.user_id:
            return s.id
    return await sources.create_source(
        ws, connector=MBOX, name=name, config={}, visibility=[user_principal(identity.user_id)],
        data_class=DataClass.PRIVATE, created_by=identity.user_id,
    )  # fmt: skip
