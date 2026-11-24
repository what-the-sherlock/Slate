"""Housekeeping the worker runs at the start of each run (cheap and bounded)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog

from core.ports.sources import SourceStore
from core.ports.storage import BlobStore

log = structlog.get_logger("maintenance")

ABANDONED_AFTER_S = 24 * 3600  # far beyond the 15-minute upload token lifetime
SWEEP_LIMIT = 200


async def sweep_abandoned_uploads(blobs: BlobStore, sources: SourceStore) -> int:
    """Delete uploaded objects nobody completed (security review: storage filling).

    An object is abandoned when it is older than a day and neither a document nor an
    unfinished job references it. Keys are deterministic, so a user may re-upload the
    same file to the same key while we sweep: right before deleting, re-check the
    object's age and its references, and skip it if either changed.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=ABANDONED_AFTER_S)
    deleted = 0
    for key in await blobs.list_older_than("uploads/", ABANDONED_AFTER_S, SWEEP_LIMIT):
        if await sources.blob_in_use(key):
            continue
        modified = await blobs.modified_at(key)
        if modified is None or modified > cutoff:
            continue  # gone, or re-uploaded since we listed it
        if await sources.blob_in_use(key):
            continue
        await blobs.delete(key)
        deleted += 1
    if deleted:
        log.info("swept_abandoned_uploads", count=deleted)
    return deleted
