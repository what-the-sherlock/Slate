"""TEMPORARY (M3 slice 0, ADR-0010): does SSE stream unbuffered through the Vercel rewrite?

Ten events, 0.5 s apart, each carrying the server's send time. `scripts/spike_sse.py`
records arrival times: ~0.5 s gaps mean streaming works; everything at once at ~5 s means
the proxy buffers. No auth, no database, no paid calls, bounded to ~5 s. Delete this
router once the result is recorded in ADR-0010.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

router = APIRouter(tags=["spike"], include_in_schema=False)

EVENTS = 10
INTERVAL_S = 0.5


@router.get("/spike/sse")
async def spike_sse() -> EventSourceResponse:
    async def events() -> AsyncIterator[dict[str, str]]:
        for n in range(EVENTS):
            yield {"event": "tick", "data": f"{n} {time.time():.3f}"}
            await asyncio.sleep(INTERVAL_S)
        yield {"event": "done", "data": "ok"}

    # X-Accel-Buffering: no asks nginx-style proxies not to buffer; harmless elsewhere.
    return EventSourceResponse(
        events(),
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        ping=15,
    )
