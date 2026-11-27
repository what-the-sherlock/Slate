"""The repository overview document (IMPROVEMENT_PLAN P1-2, ADR-0023 amendment). Pure.

Code isn't indexed (ADR-0023), so "what does this repo do?" had nothing to find in a
code-heavy repo. Each repo gets ONE small synthetic document instead: description, topics
and language; the top two levels of the tree; summaries of the root manifests
(package.json, pyproject.toml, Cargo.toml, go.mod, Dockerfile); and the latest release's
notes. It is rebuilt only when the default branch's head changes.

Manifest contents are summarised, never copied whole, and every part is capped, so a
hostile repo can't turn the overview into a large embedding bill.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, cast

from core.domain.github import SKIP_DIRS, Repo

OVERVIEW_EXTERNAL_ID = "repo:overview"
MANIFEST_NAMES = frozenset({"package.json", "pyproject.toml", "Cargo.toml", "go.mod", "Dockerfile"})
MAX_MANIFEST_BYTES = 64 * 1024
MAX_TREE_ENTRIES = 200
MAX_LIST_ITEMS = 20
MAX_RELEASE_CHARS = 4_000
MAX_FIELD_CHARS = 300


@dataclass(frozen=True, slots=True)
class TreeOverview:
    """Yielded once by ``GitHubApi.doc_files``, before ``TreeSkips``: the tree's top two
    levels (directories end with "/") and the root manifests' bytes."""

    entries: tuple[str, ...] = ()
    manifests: Mapping[str, bytes] = field(default_factory=dict[str, bytes])


@dataclass(frozen=True, slots=True)
class Release:
    tag: str
    name: str | None
    body: str
    published_at: datetime | None = None


def tree_entry(path: str, *, is_dir: bool) -> str | None:
    """How a tarball path appears in the overview's tree, or None when it's left out:
    deeper than two levels, or inside a vendored, generated or hidden directory."""
    p = PurePosixPath(path)
    if not p.parts or p.is_absolute() or ".." in p.parts or len(p.parts) > 2:
        return None
    if any(part in SKIP_DIRS or part.startswith(".") for part in p.parts):
        return None
    return f"{p.as_posix()}/" if is_dir else p.as_posix()


def is_manifest(path: str) -> bool:
    return path in MANIFEST_NAMES  # root level only: a monorepo's packages aren't listed


def overview_markdown(repo: Repo, tree: TreeOverview | None, release: Release | None) -> str:
    lines = [f"# {repo.full_name}: repository overview", ""]
    about: list[str] = []
    if repo.description:
        about.append(f"Description: {_clip(repo.description)}")
    if repo.language:
        about.append(f"Main language: {repo.language}")
    if repo.topics:
        about.append("Topics: " + ", ".join(repo.topics[:MAX_LIST_ITEMS]))
    about.append(f"Default branch: {repo.default_branch}")
    lines += about
    if tree is not None:
        for name in sorted(tree.manifests):
            summary = summarise_manifest(name, tree.manifests[name])
            if summary:
                lines += ["", f"## {name}", "", *summary]
        if tree.entries:
            shown = sorted(tree.entries)[:MAX_TREE_ENTRIES]
            lines += ["", "## Files and folders (top two levels)", "", *(f"- {e}" for e in shown)]
            if len(tree.entries) > len(shown):
                lines.append(f"- … and {len(tree.entries) - len(shown)} more")
    if release is not None:
        title = release.name or release.tag
        when = f" ({release.published_at:%Y-%m-%d})" if release.published_at else ""
        lines += ["", f"## Latest release: {_clip(title)}{when}", ""]
        body = release.body.strip()
        if len(body) > MAX_RELEASE_CHARS:
            body = body[:MAX_RELEASE_CHARS] + "\n\n(truncated)"
        lines.append(body or "(no release notes)")
    return "\n".join(lines) + "\n"


def summarise_manifest(name: str, data: bytes) -> list[str]:
    """A few facts from a manifest, as Markdown lines. [] when it can't be read: a broken
    manifest must not break the sync."""
    if len(data) > MAX_MANIFEST_BYTES:
        return []
    text = data.decode("utf-8", errors="replace")
    try:
        if name == "package.json":
            return _package_json(cast(object, json.loads(text)))
        if name == "pyproject.toml":
            return _pyproject(tomllib.loads(text))
        if name == "Cargo.toml":
            return _cargo(tomllib.loads(text))
    except (ValueError, tomllib.TOMLDecodeError):
        return []
    if name == "go.mod":
        return _go_mod(text)
    if name == "Dockerfile":
        return _dockerfile(text)
    return []


def _package_json(raw: object) -> list[str]:
    if not isinstance(raw, dict):
        return []
    d = cast(dict[str, Any], raw)
    out = _facts(d, ("name", "description", "version"))
    out += _keys("Scripts", d.get("scripts"))
    out += _keys("Dependencies", d.get("dependencies"))
    return out


def _pyproject(raw: dict[str, Any]) -> list[str]:
    project = raw.get("project")
    if not isinstance(project, dict):
        return []
    p = cast(dict[str, Any], project)
    out = _facts(p, ("name", "description", "version", "requires-python"))
    out += _items("Dependencies", p.get("dependencies"))
    out += _keys("Scripts", p.get("scripts"))
    return out


def _cargo(raw: dict[str, Any]) -> list[str]:
    package = raw.get("package")
    out = (
        _facts(cast(dict[str, Any], package), ("name", "description", "version"))
        if isinstance(package, dict)
        else []
    )
    return out + _keys("Dependencies", raw.get("dependencies"))


def _go_mod(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines()[:200]:
        words = line.split()
        if len(words) == 2 and words[0] in ("module", "go"):
            out.append(f"{'Module' if words[0] == 'module' else 'Go version'}: {_clip(words[1])}")
    return out


def _dockerfile(text: str) -> list[str]:
    bases = [
        _clip(line.split(None, 1)[1])
        for line in text.splitlines()[:500]
        if line.strip().upper().startswith("FROM ") and len(line.split(None, 1)) == 2
    ]
    return [f"Base image: {b}" for b in bases[:5]]


def _facts(d: Mapping[str, Any], keys: Sequence[str]) -> list[str]:
    return [
        f"{k.replace('-', ' ').capitalize()}: {_clip(str(d[k]))}"
        for k in keys
        if isinstance(d.get(k), str | int | float) and str(d[k]).strip()
    ]


def _keys(label: str, value: object) -> list[str]:
    if not isinstance(value, dict) or not value:
        return []
    names = [_clip(str(k)) for k in list(cast(dict[str, Any], value))[:MAX_LIST_ITEMS]]
    return [f"{label}: " + ", ".join(names)]


def _items(label: str, value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        return []
    names = [_clip(str(v)) for v in cast(list[Any], value)[:MAX_LIST_ITEMS]]
    return [f"{label}: " + ", ".join(names)]


def _clip(value: str) -> str:
    one_line = " ".join(value.split())
    return one_line if len(one_line) <= MAX_FIELD_CHARS else one_line[:MAX_FIELD_CHARS] + "…"
