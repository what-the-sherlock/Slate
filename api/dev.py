"""Local dev server: `uv run python -m api.dev [--port 8000] [--reload]`.

Same as `uvicorn api.main:app`, except on Windows it selects the event loop psycopg's
async mode needs (Selector, not Proactor) and tells uvicorn not to replace it.
"""

from __future__ import annotations

import argparse

import uvicorn

from core.adapters.postgres.engine import use_selector_loop_on_windows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    use_selector_loop_on_windows()
    uvicorn.run("api.main:app", host=args.host, port=args.port, reload=args.reload, loop="none")


if __name__ == "__main__":
    main()
