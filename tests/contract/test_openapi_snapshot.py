"""The committed openapi.json is the contract the web client is generated from."""

import pytest

from scripts.export_openapi import TARGET, render

pytestmark = pytest.mark.contract


def test_openapi_snapshot_is_current() -> None:
    assert TARGET.exists(), "run: uv run python scripts/export_openapi.py"
    assert TARGET.read_text(encoding="utf-8") == render(), (
        "API changed: run `uv run python scripts/export_openapi.py` and commit openapi.json"
    )


def test_every_route_has_summary_and_tags() -> None:
    import json

    spec = json.loads(render())
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            assert op.get("summary"), f"{method.upper()} {path} has no summary"
            assert op.get("tags"), f"{method.upper()} {path} has no tags"
