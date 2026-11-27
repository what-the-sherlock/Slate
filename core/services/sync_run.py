"""One run boundary for every sync engine (P0-1).

Every sync records an ``ingestion_runs`` row, saves its checkpoint and finishes the run.
Each engine used to hand-write that ``try/except/finally``. Two copies (GitHub, Discord)
caught only known errors, so any other exception still ran ``finally`` and recorded the
crash as **succeeded, 0 seen**, which also moved ``last_synced_at``.

``RunBoundary`` is an async context manager, so it sees *every* way the body can exit:

* normal exit: ``succeeded`` (or whatever the engine set with ``partial``/``fail``);
* ``BudgetExceeded``: ``partial`` (resumable), swallowed;
* ``RateLimited``: ``partial``, then re-raised so the queue backs off and retries;
* any other ``AppError``: ``failed`` with its user-facing detail, recorded, swallowed;
* any other ``Exception``: ``failed`` with only the exception *type* (its message could
  contain document text, rule 5), recorded as retryable, logged with where it was raised,
  then re-raised so the job is retried and ends ``dead`` if it keeps failing;
* cancellation (``asyncio.CancelledError``, e.g. the inline time limit) and other
  ``BaseException``s: ``partial`` ("interrupted"), never ``succeeded``, then re-raised.

``last_synced_at`` moves only when the pass itself completed. A run whose only problem is
a few failed items still counts (it's due again in an hour, not on every worker pass; the
engine's held-back checkpoint retries those items). The checkpoint saved is whatever the
engine last committed, so progress made before a crash isn't redone.
"""

from __future__ import annotations

import asyncio
import traceback
from collections.abc import Callable, Mapping
from pathlib import PurePath
from types import TracebackType
from typing import Protocol

import structlog

from core.domain.errors import AppError, BudgetExceeded, RateLimited
from core.domain.ids import RunId, SourceId
from core.domain.skips import Skips
from core.ports.sources import SourceStore

log = structlog.get_logger(__name__)

INTERRUPTED = "Interrupted before it finished; the rest continues on the next attempt."


def crashed_summary(exc: BaseException) -> str:
    return f"Sync stopped unexpectedly ({type(exc).__name__}). It will be retried."


class Counts(Protocol):
    seen: int
    changed: int
    failed: int


class RunBoundary:
    def __init__(
        self,
        sources: SourceStore,
        source_id: SourceId,
        run_id: RunId,
        *,
        event: str,
        counts: Callable[[], Counts],
        checkpoint: Callable[[], dict[str, object]] | None,
        failed_noun: str = "item(s)",
        rate_limited_summary: str | None = None,
        log_fields: Callable[[], Mapping[str, object]] | None = None,
    ) -> None:
        self._sources = sources
        self._source_id = source_id
        self.run = run_id
        self._event = event
        self._counts = counts
        self._checkpoint = checkpoint
        self._noun = failed_noun
        self._rate_summary = rate_limited_summary
        self._log_fields = log_fields
        self.status = "succeeded"
        self.summary: str | None = None
        self.skips = Skips()

    # ------------------------------------------------------------ engine-set outcomes

    def partial(self, summary: str) -> None:
        self.status, self.summary = "partial", summary

    async def fail(
        self, summary: str, *, record: bool = True, retryable: bool = False, stage: str = "fetch"
    ) -> None:
        self.status, self.summary = "failed", summary
        if record:
            await self._sources.record_error(
                self.run, external_id=None, stage=stage, message=summary, retryable=retryable
            )

    # ------------------------------------------------------------ context manager

    async def __aenter__(self) -> RunBoundary:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        reraise = exc is not None
        if exc is None:
            pass
        elif isinstance(exc, BudgetExceeded):
            self.partial(exc.detail)
            reraise = False
        elif isinstance(exc, RateLimited):
            self.partial(self._rate_summary or exc.detail)  # re-raised: the queue backs off
        elif isinstance(exc, AppError):
            await self.fail(exc.detail, retryable=exc.retryable)
            reraise = False
        elif isinstance(exc, Exception):
            await self._crashed(exc)
        else:  # CancelledError, KeyboardInterrupt, SystemExit: never "succeeded"
            self.partial(INTERRUPTED)
        await self._finish(interrupted=exc is not None and not isinstance(exc, Exception))
        return not reraise

    async def _crashed(self, exc: Exception) -> None:
        summary = crashed_summary(exc)
        frame = _origin(exc)
        log.error(
            f"{self._event}_crashed",
            error_type=type(exc).__name__,
            where=frame,  # module:function:line, never the message (rule 5)
        )
        await self.fail(summary, retryable=True)

    async def _finish(self, *, interrupted: bool) -> None:
        async def finish() -> None:
            c = self._counts()
            if self._checkpoint is not None:
                await self._sources.save_checkpoint(
                    self._source_id, self._checkpoint(), synced=self.status == "succeeded"
                )
            if c.failed and self.status == "succeeded":
                self.partial(f"{c.failed} {self._noun} couldn't be indexed.")
            await self._sources.finish_run(
                self.run,
                status=self.status,
                seen=c.seen,
                changed=c.changed,
                failed=c.failed,
                error_summary=self.summary,
                skipped=self.skips.as_json(),
            )
            extra = dict(self._log_fields()) if self._log_fields else {}
            log.info(
                self._event,
                status=self.status,
                seen=c.seen,
                changed=c.changed,
                failed=c.failed,
                skipped=self.skips.total,
                **extra,
            )

        if interrupted:
            # A cancelled task may be cancelled again while it records its outcome; shield
            # the bookkeeping so the run never stays "running" forever.
            await asyncio.shield(finish())
        else:
            await finish()


def _origin(exc: BaseException) -> str:
    """Where an exception was raised, as ``file:function:line`` (no message, no locals)."""
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return "unknown"
    f = frames[-1]
    return f"{PurePath(f.filename).name}:{f.name}:{f.lineno}"
