"""Measure API latency and DB connections per request against a running API.

    uv run python -m api.dev           # in one terminal
    uv run python scripts/bench_api.py [--base http://127.0.0.1:8000] [-n 20]

Reads only the ``Server-Timing`` header the API sets (db-connect count and time), so
it works against local or deployed APIs. Uses a guest session for the signed-in
routes when the workspace has guest mode, and signs out at the end. No paid APIs.
"""

from __future__ import annotations

import argparse
import re
import statistics
import time

import httpx

_TIMING = re.compile(r'db-connect;dur=([\d.]+);desc="(\d+) new"')
_HEADERS = {"X-Requested-With": "slate"}


def _sample(client: httpx.Client, path: str, n: int) -> None:
    totals: list[float] = []
    connects: list[int] = []
    connect_ms: list[float] = []
    status = 0
    for _ in range(n):
        started = time.perf_counter()
        resp = client.get(path)
        totals.append((time.perf_counter() - started) * 1000)
        status = resp.status_code
        match = _TIMING.search(resp.headers.get("server-timing", ""))
        if match:
            connect_ms.append(float(match.group(1)))
            connects.append(int(match.group(2)))
    totals.sort()
    p95 = totals[max(0, int(len(totals) * 0.95) - 1)]
    print(
        f"{path:<28} {status}  p50={statistics.median(totals):7.0f}ms  p95={p95:7.0f}ms  "
        f"connects/req={statistics.mean(connects) if connects else float('nan'):.1f}  "
        f"connect_ms/req={statistics.mean(connect_ms) if connect_ms else float('nan'):6.0f}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("-n", type=int, default=20)
    args = parser.parse_args()

    with httpx.Client(base_url=f"{args.base}/api/v1", headers=_HEADERS, timeout=30) as c:
        c.get("/healthz")  # warm the process (first import, container build)
        for path in ("/healthz", "/readyz", "/auth/status"):
            _sample(c, path, args.n)
        if c.post("/auth/guest").status_code == 200:
            for path in ("/me", "/sources"):
                _sample(c, path, args.n)
            csrf = next((v for k, v in c.cookies.items() if "csrf" in k.lower()), "")
            c.post("/auth/logout", headers={"X-CSRF-Token": csrf})
        else:
            print("guest mode unavailable: skipped signed-in routes")


if __name__ == "__main__":
    main()
