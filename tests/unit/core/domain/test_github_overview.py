"""The repository overview document (P1-2): pure rules, fixture tree and manifests."""

from __future__ import annotations

from datetime import UTC, datetime

from core.domain.github import Repo
from core.domain.github_overview import (
    MAX_FIELD_CHARS,
    MAX_MANIFEST_BYTES,
    Release,
    TreeOverview,
    is_manifest,
    overview_markdown,
    summarise_manifest,
    tree_entry,
)

REPO = Repo(
    1, "acme/wave", False, "main", "https://github.com/acme/wave",
    description="Realtime wave simulation", language="TypeScript", topics=("physics", "webgl"),
)  # fmt: skip
PACKAGE = b'{"name": "wave", "description": "Simulates waves", "scripts": {"dev": "vite"},'
PACKAGE += b' "dependencies": {"three": "^0.160"}}'
PYPROJECT = (
    b'[project]\nname = "wave-tools"\nrequires-python = ">=3.12"\ndependencies = ["numpy"]\n'
)


def test_the_overview_says_what_the_repo_is_and_holds() -> None:
    manifests = {"package.json": PACKAGE, "pyproject.toml": PYPROJECT, "Dockerfile": b"FROM node"}
    tree = TreeOverview(("src/", "src/main.ts", "package.json", "README.md"), manifests)
    release = Release(
        "v1.2.0", "Faster shaders", "Halved GPU time.", datetime(2026, 11, 1, tzinfo=UTC)
    )
    text = overview_markdown(REPO, tree, release)
    for fact in (
        "# acme/wave: repository overview",
        "Description: Realtime wave simulation",
        "Main language: TypeScript",
        "Topics: physics, webgl",
        "Scripts: dev",
        "Dependencies: three",
        "Requires python: >=3.12",
        "Dependencies: numpy",
        "Base image: node",
        "- src/main.ts",
        "## Latest release: Faster shaders (2026-11-01)",
        "Halved GPU time.",
    ):
        assert fact in text, fact


def test_without_a_tree_or_release_the_overview_still_has_the_basics() -> None:
    text = overview_markdown(REPO, None, None)
    assert "Main language: TypeScript" in text
    assert "Latest release" not in text
    assert "Files and folders" not in text


def test_the_tree_keeps_two_levels_and_leaves_out_vendored_and_hidden_folders() -> None:
    assert tree_entry("src", is_dir=True) == "src/"
    assert tree_entry("src/app.py", is_dir=False) == "src/app.py"
    assert tree_entry("src/a/b.py", is_dir=False) is None  # three levels
    assert tree_entry("node_modules/x", is_dir=False) is None
    assert tree_entry(".github/workflows", is_dir=True) is None
    assert tree_entry("../etc", is_dir=True) is None


def test_broken_or_huge_manifests_are_ignored_not_fatal() -> None:
    assert summarise_manifest("package.json", b"{not json") == []
    assert summarise_manifest("package.json", b"[1, 2]") == []
    assert summarise_manifest("pyproject.toml", b"[project\nname=") == []
    assert summarise_manifest("package.json", b" " * (MAX_MANIFEST_BYTES + 1)) == []
    assert summarise_manifest("go.mod", b"module example.com/wave\n\ngo 1.22\n") == [
        "Module: example.com/wave",
        "Go version: 1.22",
    ]


def test_only_root_manifests_count_and_fields_are_clipped() -> None:
    assert is_manifest("package.json")
    assert not is_manifest("packages/ui/package.json")
    long = b'{"description": "' + b"x" * 5000 + b'"}'
    [line] = summarise_manifest("package.json", long)
    assert len(line) < MAX_FIELD_CHARS + 30
