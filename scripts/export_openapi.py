"""Write the API's OpenAPI document to openapi.json (used by `pnpm gen:api` and CI drift check).

uv run python scripts/export_openapi.py            # write openapi.json
uv run python scripts/export_openapi.py --check    # exit 1 if openapi.json is stale
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api.main import create_app  # noqa: E402

TARGET = ROOT / "openapi.json"


def render() -> str:
    return json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    current = render()
    if args.check:
        if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != current:
            print("openapi.json is stale: run `uv run python scripts/export_openapi.py`")
            return 1
        print("openapi.json is up to date")
        return 0
    TARGET.write_text(current, encoding="utf-8", newline="\n")
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
