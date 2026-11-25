"""Check whether SSE streams unbuffered (ADR-0010 spike). Read-only, free.

    uv run python scripts/spike_sse.py https://slate-livid-nine.vercel.app   # through the rewrite
    uv run python scripts/spike_sse.py http://127.0.0.1:8000                  # API directly

Prints each event's arrival time and a verdict. Streaming works when events arrive
spread out (~0.5 s apart); it's buffered when they all arrive together at the end.
"""

from __future__ import annotations

import sys
import time

import httpx


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
    url = f"{base}/api/v1/spike/sse"
    arrivals: list[float] = []
    start = time.perf_counter()
    with httpx.stream("GET", url, timeout=httpx.Timeout(30.0, connect=10.0)) as r:
        print(f"{r.status_code} {url}")
        print(
            "headers:",
            {
                k: v
                for k, v in r.headers.items()
                if k.lower()
                in {
                    "content-type",
                    "content-encoding",
                    "transfer-encoding",
                    "x-vercel-cache",
                    "server",
                }
            },
        )
        for line in r.iter_lines():
            if line.startswith("event: tick"):
                arrivals.append(time.perf_counter() - start)
                print(f"  tick {len(arrivals) - 1:2d} arrived at {arrivals[-1]:5.2f} s")
    if len(arrivals) < 2:
        print("VERDICT: no events received; check the URL and that the API deployed")
        return 2
    spread = arrivals[-1] - arrivals[0]
    first = arrivals[0]
    print(f"first event after {first:.2f} s; first-to-last spread {spread:.2f} s")
    if spread >= 3.0:
        print("VERDICT: STREAMING (events arrived over time; the rewrite does not buffer)")
        return 0
    print("VERDICT: BUFFERED (events arrived together; the proxy held the response)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
