"""The ``sync_discord`` job handler (ADR-0027), for the worker and the inline runner.

Per connected channel:

* **Messages** (text and announcement channels). The first sync reads backwards from the
  newest message for 90 days or 5,000 messages, so a busy channel keeps its *latest*
  history. Later syncs read forwards from a re-read anchor: the first remembered window
  ending in the last 48 hours (or the newest window). Messages are grouped into windows
  (``group_windows``); each window is one document, skipped by checksum when unchanged.
  After a complete pass, windows that started inside the re-read range and no longer exist
  (messages deleted, a window split) are pruned.
* **Threads** (any channel, forum posts included; never private threads). Threads with
  activity after the channel's ``threads_floor`` are re-read whole, oldest activity first.
  After a complete pass, thread documents active inside the range but no longer listed
  (deleted threads) are pruned.

Failures are isolated: a channel the bot lost access to is reported and skipped (its
documents stay, like a GitHub repo the app can't see); a deleted channel's documents are
removed. A failed document stops that channel's cursor at the last good one, so the next
sync retries it; unchanged ones re-index for free. Progress is saved after every channel
and on any exit, so a deadline, rate limit or crash resumes where it stopped.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

import structlog

from core.domain.discord import (
    BACKFILL_DAYS,
    MAX_MESSAGES_PER_CHANNEL_PER_SYNC,
    MAX_THREAD_MESSAGES,
    MAX_THREADS_PER_SYNC,
    REREAD,
    Channel,
    ChannelCursor,
    Guild,
    Message,
    SyncCheckpoint,
    Thread,
    Window,
    authors,
    can_read,
    channel_prefix,
    checksum,
    group_windows,
    intent_looks_off,
    message_url,
    recent_windows,
    snowflake_at,
    snowflake_time,
    thread_external_id,
    thread_prefix,
    thread_sections,
    thread_title,
    window_external_id,
    window_sections,
    window_title,
)
from core.domain.documents import DocumentUpsert, Section
from core.domain.errors import (
    AppError,
    BudgetExceeded,
    NotFound,
    PermissionDenied,
    RateLimited,
    UpstreamUnavailable,
)
from core.domain.ids import RunId
from core.domain.jobs import Job
from core.ports.discord import DiscordApi
from core.ports.jobs import JobQueue
from core.ports.sources import SourceRecord, SourceStore
from core.ports.system import Clock
from core.services.discord import CONNECTOR, SYNC_DISCORD, channel_configs, parse_source_id
from core.services.ingest import IngestService, Stage

log = structlog.get_logger(__name__)

INLINE_SYNC_BUDGET_S = 90.0  # inside the inline runner's 120 s
MIME = "text/plain"
INTENT_OFF = (
    "Discord returns messages without their text: turn on the Message Content intent "
    "for Slate's bot in the Discord developer portal."
)


class _OutOfTime(Exception):
    """The deadline passed: stop cleanly, save progress, continue in the background."""


class _IntentOff(Exception):
    pass


@dataclass
class _Tally:
    seen: int = 0
    changed: int = 0
    failed: int = 0
    pruned: int = 0
    threads: int = 0  # threads read this run, against MAX_THREADS_PER_SYNC


@dataclass
class _Run:
    source: SourceRecord
    run: RunId
    guild: Guild
    now: datetime
    out_of_time: Callable[[], bool]
    cp: SyncCheckpoint
    tally: _Tally = field(default_factory=_Tally)
    channel_names: Mapping[str, str] = field(default_factory=dict[str, str])
    role_names: Mapping[str, str] = field(default_factory=dict[str, str])
    incomplete: bool = False  # a cap was hit: more remains for the next sync

    def check_time(self) -> None:
        if self.out_of_time():
            raise _OutOfTime


class DiscordSync:
    def __init__(
        self,
        discord: DiscordApi,
        sources: SourceStore,
        jobs: JobQueue,
        ingest: IngestService,
        clock: Clock,
    ) -> None:
        self._dc = discord
        self._sources = sources
        self._jobs = jobs
        self._ingest = ingest
        self._clock = clock

    async def handle(self, job: Job) -> None:
        await self.sync(job, deadline=None)

    async def handle_inline(self, job: Job) -> None:
        await self.sync(job, deadline=time.monotonic() + INLINE_SYNC_BUDGET_S)

    async def sync(self, job: Job, *, deadline: float | None) -> None:
        source = await self._sources.get_source(
            job.workspace_id, parse_source_id(job.payload.get("source_id"))
        )
        if source is None or source.connector != CONNECTOR or source.status != "active":
            log.info("discord.sync_skipped", reason="source gone or paused")
            return
        cp = SyncCheckpoint.from_json(source.checkpoint)
        run_id = await self._sources.create_run(
            source.id, "full" if cp.first_sync else "incremental"
        )
        out_of_time = (lambda: False) if deadline is None else (lambda: time.monotonic() > deadline)
        state: _Run | None = None
        status, summary, retry = "succeeded", None, None
        tally = _Tally()
        try:
            guild_id = str(source.config.get("guild_id", ""))
            guild = await self._dc.guild(guild_id)
            state = _Run(source, run_id, guild, self._clock.now(), out_of_time, cp, tally)
            await self._sync_guild(state)
            if state.incomplete:
                status, summary = "partial", "More to read: the rest continues on the next sync."
        except _OutOfTime:
            status, summary = "partial", "Still syncing: the rest continues in the background."
            await self._jobs.enqueue(
                source.workspace_id,
                SYNC_DISCORD,
                {"source_id": str(source.id)},
                f"{SYNC_DISCORD}:{source.id}:cont:{run_id}",
            )
        except BudgetExceeded as e:
            status, summary = "partial", e.detail
        except RateLimited as e:
            status, summary, retry = (
                "partial",
                "Discord's rate limit was reached; syncing resumes later.",
                e,
            )
        except _IntentOff:
            status, summary = "failed", INTENT_OFF
            await self._sources.record_error(
                run_id, external_id=None, stage="fetch", message=INTENT_OFF, retryable=False
            )
        except (NotFound, PermissionDenied) as e:
            status, summary = "failed", e.detail
            await self._sources.record_error(
                run_id, external_id=None, stage="fetch", message=e.detail, retryable=False
            )
        except UpstreamUnavailable as e:
            status, summary = "failed", e.detail
        finally:
            final = state.cp if state is not None else cp
            await self._sources.save_checkpoint(
                source.id, final.as_json(), synced=status == "succeeded"
            )
            if tally.failed and status == "succeeded":
                status, summary = "partial", f"{tally.failed} item(s) couldn't be indexed."
            await self._sources.finish_run(
                run_id,
                status=status,
                seen=tally.seen,
                changed=tally.changed,
                failed=tally.failed,
                error_summary=summary,
            )
            log.info(
                "discord.synced",
                status=status,
                seen=tally.seen,
                changed=tally.changed,
                pruned=tally.pruned,
            )
        if retry is not None:
            raise retry  # the queue backs off and runs the job again

    # ------------------------------------------------------------------ per server

    async def _sync_guild(self, st: _Run) -> None:
        live = {c.id: c for c in await self._dc.channels(st.guild.id)}
        bot = await self._dc.bot_member(st.guild.id)
        st.channel_names = {cid: c.name for cid, c in live.items()}
        st.role_names = {r.id: r.name for r in st.guild.roles}
        wanted = channel_configs(st.source)
        readable: list[Channel] = []
        for cid, _name in wanted:
            st.check_time()
            channel = live.get(cid)
            if channel is None:
                await self._channel_gone(st, cid)
                continue
            if not can_read(st.guild, bot, channel):
                st.tally.failed += 1
                await self._sources.record_error(
                    st.run,
                    external_id=f"channel:{cid}",
                    stage="fetch",
                    message=f"Slate's bot can't read #{channel.name} anymore.",
                    retryable=False,
                )
                continue
            readable.append(channel)
        for channel in readable:
            if channel.has_messages:
                await self._channel_messages(st, channel)
        if readable:
            active = [t for t in await self._dc.active_threads(st.guild.id) if not t.private]
            for channel in readable:
                await self._channel_threads(st, channel, active)

    async def _channel_gone(self, st: _Run, channel_id: str) -> None:
        """Deleted on Discord: its documents go too (the content no longer exists)."""
        gone = [
            *await self._sources.document_checksums(st.source.id, channel_prefix(channel_id)),
            *await self._sources.document_checksums(st.source.id, thread_prefix(channel_id)),
        ]
        st.tally.pruned += await self._sources.delete_documents(st.source.id, gone)
        st.cp = st.cp.without(channel_id)
        await self._sources.record_error(
            st.run,
            external_id=f"channel:{channel_id}",
            stage="fetch",
            message="This channel no longer exists on Discord; its messages were removed.",
            retryable=False,
        )

    # ------------------------------------------------------------------ messages

    async def _channel_messages(self, st: _Run, channel: Channel) -> None:
        cursor = st.cp.cursor(channel.id)
        cutoff = snowflake_at(st.now - REREAD)
        cap = MAX_MESSAGES_PER_CHANNEL_PER_SYNC
        try:
            if cursor.last_id is None:
                since = snowflake_at(st.now - timedelta(days=BACKFILL_DAYS))
                messages = await self._dc.latest_messages(channel.id, since, cap)
                start = (int(messages[0].id) - 1) if messages else since
                capped = False  # older history past the cap is skipped on purpose
            else:
                anchor = None if cursor.backlog else cursor.anchor(cutoff)
                # A capped pass resumes from where it stopped (no re-read): otherwise a
                # channel busier than the cap would re-read the same messages every hour.
                start = int(anchor) - 1 if anchor else int(cursor.last_id)
                messages = [m async for m in self._dc.messages_after(channel.id, start, cap)]
                capped = len(messages) >= cap
        except PermissionDenied as e:
            await self._channel_error(st, channel, e.detail)
            return
        except NotFound as e:
            # It was in this run's channel list a moment ago: one 404 isn't proof enough to
            # delete its documents. Report it; the next sync's list decides.
            await self._channel_error(st, channel, e.detail)
            return
        if intent_looks_off(messages):
            raise _IntentOff
        st.check_time()
        windows = group_windows(messages)
        existing = await self._sources.document_versions(st.source.id, channel_prefix(channel.id))
        produced: set[str] = set()
        ok: list[Window] = []
        stuck = False
        try:
            for w in windows:
                st.check_time()
                ext = window_external_id(channel.id, w.first_id)
                produced.add(ext)
                sections = window_sections(
                    w,
                    guild_name=st.guild.name,
                    channel_name=channel.name,
                    channels=st.channel_names,
                    roles=st.role_names,
                )
                title = window_title(channel.name, w)
                indexed = await self._index(
                    st, ext, sections, title=title,
                    uri=message_url(st.guild.id, channel.id, w.first_id),
                    existing=existing.get(ext, (None, None))[0],
                    author=authors(w.messages), created=w.messages[0].at,
                    updated=w.messages[-1].at,
                    metadata={"guild": st.guild.id, "channel": channel.id, "kind": "messages",
                              "messages": len(w.messages)},
                )  # fmt: skip
                if not indexed:
                    stuck = True
                    break
                ok.append(w)
        finally:
            st.cp = st.cp.with_cursor(
                channel.id, self._advance(cursor, messages, windows, ok, stuck, cutoff, start)
            )
        st.cp = st.cp.with_cursor(
            channel.id, replace(st.cp.cursor(channel.id), backlog=capped and not stuck)
        )
        if capped:
            st.incomplete = True
        elif not stuck:
            # Complete up to the newest message: anything that started in the re-read range
            # and wasn't produced again has been deleted (or merged into another window).
            stale = [
                e for e in existing
                if e not in produced and _first_id(e) is not None and _first_id(e) > start  # type: ignore[operator]
            ]  # fmt: skip
            st.tally.pruned += await self._sources.delete_documents(st.source.id, stale)

    @staticmethod
    def _advance(
        cursor: ChannelCursor,
        messages: Sequence[Message],
        windows: Sequence[Window],
        ok: Sequence[Window],
        stuck: bool,
        cutoff: int,
        start: int,
    ) -> ChannelCursor:
        """Where this channel resumes. All good: after the newest message read (including
        ones with nothing to index). Otherwise: after the last window indexed, and the
        unfinished one is remembered so the next sync re-reads from it."""
        # Windows this pass re-read were rebuilt from scratch; only older ones carry over.
        previous = tuple(w for w in cursor.recent if int(w[0]) <= start)
        if len(ok) == len(windows) and not stuck:
            newest = max((int(m.id) for m in messages), default=None)
            last = str(newest) if newest is not None else cursor.last_id
            if cursor.last_id and last and int(cursor.last_id) > int(last):
                last = cursor.last_id
            return replace(cursor, last_id=last, recent=recent_windows(windows, cutoff, previous))
        done = list(ok)
        pending = windows[len(ok)] if len(ok) < len(windows) else None
        remembered = [*done, pending] if pending is not None else done
        last = ok[-1].last_id if ok else cursor.last_id
        recent = recent_windows(remembered, cutoff, previous)
        if pending is not None and (not recent or recent[-1][0] != pending.first_id):
            recent = (*recent, (pending.first_id, pending.last_id))
        # The re-read anchor must not be after the unfinished window.
        if pending is not None:
            recent = tuple(w for w in recent if int(w[0]) <= int(pending.first_id)) or (
                (pending.first_id, pending.last_id),
            )
        if last is None and pending is not None:
            last = str(int(pending.first_id) - 1)
        return replace(cursor, last_id=last, recent=recent)

    async def _channel_error(self, st: _Run, channel: Channel, detail: str) -> None:
        st.tally.failed += 1
        await self._sources.record_error(
            st.run, external_id=f"channel:{channel.id}", stage="fetch", message=detail,
            retryable=False,
        )  # fmt: skip

    # ------------------------------------------------------------------ threads

    async def _channel_threads(self, st: _Run, channel: Channel, active: Sequence[Thread]) -> None:
        cursor = st.cp.cursor(channel.id)
        pass_start = st.now
        floor = (
            cursor.threads_floor
            if cursor.threads_floor is not None
            else snowflake_at(st.now - timedelta(days=BACKFILL_DAYS))
        )
        since = snowflake_time(floor)
        try:
            found: dict[str, Thread] = {t.id: t for t in active if t.parent_id == channel.id}
            async for t in self._dc.archived_threads(channel.id, since):
                st.check_time()
                if not t.private:
                    found.setdefault(t.id, t)
        except PermissionDenied as e:
            await self._channel_error(st, channel, e.detail)
            return
        except NotFound as e:
            await self._channel_error(st, channel, e.detail)
            return
        todo = sorted(
            (t for t in found.values() if t.last_activity > floor), key=lambda t: t.last_activity
        )
        existing = await self._sources.document_versions(st.source.id, thread_prefix(channel.id))
        mark: int | None = None  # activity of the last thread fully indexed
        complete, stuck, finished = True, False, False
        try:
            for t in todo:
                st.check_time()
                if st.tally.threads >= MAX_THREADS_PER_SYNC:
                    complete = False
                    st.incomplete = True
                    break
                st.tally.threads += 1
                result = await self._thread(st, channel, t, existing)
                if result is None:  # deleted between listing and reading: nothing to keep
                    continue
                if not result:
                    stuck = True
                    break
                mark = t.last_activity
            finished = True
        finally:
            # Only a pass that ran to the end moves the floor up to the re-read period. A
            # cap, a failed thread, the deadline, a rate limit or the budget leave it at the
            # last thread indexed, so nothing listed is ever skipped.
            if finished and complete and not stuck:
                new_floor = snowflake_at(pass_start - REREAD)
            else:
                new_floor = mark if mark is not None else floor
            st.cp = st.cp.with_cursor(channel.id, replace(cursor, threads_floor=new_floor))
        if finished and complete and not stuck:
            listed = {thread_external_id(channel.id, t.id) for t in found.values()}
            stale = [
                ext for ext, (_sum, updated) in existing.items()
                if ext not in listed and updated is not None and updated >= since
            ]  # fmt: skip
            st.tally.pruned += await self._sources.delete_documents(st.source.id, stale)

    async def _thread(
        self,
        st: _Run,
        channel: Channel,
        thread: Thread,
        existing: Mapping[str, tuple[str, datetime | None]],
    ) -> bool | None:
        """Index one thread. True ok, False failed (stop here), None vanished."""
        ext = thread_external_id(channel.id, thread.id)
        try:
            messages = [m async for m in self._dc.messages_after(thread.id, 0, MAX_THREAD_MESSAGES)]
        except NotFound:
            st.tally.pruned += await self._sources.delete_documents(st.source.id, [ext])
            return None
        except PermissionDenied as e:
            st.tally.failed += 1
            await self._sources.record_error(
                st.run, external_id=ext, stage="fetch", message=e.detail, retryable=False
            )
            return False
        if intent_looks_off(messages):
            raise _IntentOff
        sections = thread_sections(
            thread,
            messages,
            guild_name=st.guild.name,
            parent_name=channel.name,
            channels=st.channel_names,
            roles=st.role_names,
        )
        if not sections or not any(s.text.strip() for s in sections):
            return True  # nothing to index (e.g. only system messages)
        return await self._index(
            st, ext, sections, title=thread_title(thread, channel.name),
            uri=message_url(st.guild.id, thread.id), existing=existing.get(ext, (None, None))[0],
            author=authors(messages), created=snowflake_time(thread.id),
            updated=snowflake_time(thread.last_activity),
            metadata={"guild": st.guild.id, "channel": channel.id, "thread": thread.id,
                      "kind": "thread", "messages": len(messages)},
        )  # fmt: skip

    # ------------------------------------------------------------------ indexing

    async def _index(
        self,
        st: _Run,
        external_id: str,
        sections: tuple[Section, ...],
        *,
        title: str,
        uri: str,
        existing: str | None,
        author: str | None,
        created: datetime,
        updated: datetime,
        metadata: dict[str, object],
    ) -> bool:
        """True when indexed (or unchanged). BudgetExceeded propagates: nothing more can be
        embedded today, so the run stops as partial with progress saved."""
        st.tally.seen += 1
        digest = checksum(sections, title)
        if existing == digest:
            return True  # unchanged: no write, no spend
        stage = Stage("chunk")
        try:
            result = await self._ingest.index_document(
                st.source.workspace_id,
                DocumentUpsert(
                    source_id=st.source.id,
                    external_id=external_id,
                    uri=uri,
                    title=title,
                    mime=MIME,
                    checksum=digest,
                    acl=st.source.visibility,
                    author=author,
                    source_created_at=created,
                    source_updated_at=updated,
                    metadata=metadata,
                ),
                sections,
                data_class=st.source.data_class,
                stage=stage,
            )
        except BudgetExceeded:
            raise
        except AppError as e:
            st.tally.failed += 1
            await self._sources.record_error(
                st.run,
                external_id=external_id,
                stage=stage.name,
                message=e.detail,
                retryable=e.retryable,
            )
            return False
        if result.changed:
            st.tally.changed += 1
        return True


def _first_id(external_id: str) -> int | None:
    """The first message id in a window's external id (``msg:<channel>:<first>``)."""
    parts = external_id.split(":")
    return int(parts[2]) if len(parts) == 3 and parts[2].isdigit() else None
