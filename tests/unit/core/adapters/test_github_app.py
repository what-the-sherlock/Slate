"""GitHubApp adapter over httpx.MockTransport: no network, no real keys."""

from __future__ import annotations

import base64
import io
import json
import tarfile
from collections.abc import Callable

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from core.adapters.github.app import GitHubApp, app_jwt
from core.domain.errors import NotFound, PermissionDenied, RateLimited
from core.domain.github import RepoFile, TreeSkips
from core.domain.github_overview import TreeOverview

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
TOKEN_JSON = {"token": "ghs_inst", "expires_at": "2099-01-01T00:00:00Z"}
Handler = Callable[[httpx.Request], httpx.Response]


def make(handler: Handler) -> tuple[GitHubApp, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json=TOKEN_JSON)
        return handler(req)

    client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    app = GitHubApp(
        app_id="123", slug="slate-app", client_id="cid", client_secret="csec",
        private_key=PEM, client=client,
    )  # fmt: skip
    return app, seen


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_app_jwt_is_rs256_signed_and_short_lived() -> None:
    token = app_jwt("123", PEM, now=1_000_000)
    head, body, sig = token.split(".")
    assert json.loads(_unb64(head)) == {"alg": "RS256", "typ": "JWT"}
    claims = json.loads(_unb64(body))
    assert claims["iss"] == "123"
    assert claims["exp"] - claims["iat"] <= 600
    KEY.public_key().verify(
        _unb64(sig), f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )


def test_install_url_carries_the_state() -> None:
    app, _ = make(lambda r: httpx.Response(404))
    assert (
        app.install_url("a b") == "https://github.com/apps/slate-app/installations/new?state=a%20b"
    )
    assert app.configured


async def test_installation_token_is_cached() -> None:
    repo = {"id": 1, "full_name": "o/r", "private": True, "default_branch": "main", "html_url": "u"}
    app, seen = make(lambda r: httpx.Response(200, json=repo))
    await app.repo(9, "o/r")
    await app.repo(9, "o/r")
    assert sum(1 for r in seen if r.url.path.endswith("/access_tokens")) == 1
    assert seen[-1].headers["authorization"] == "Bearer ghs_inst"


async def test_user_must_own_the_installation_and_the_token_is_revoked() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "ghu_user"})
        if req.url.path == "/user/installations":
            return httpx.Response(200, json={"installations": [{"id": 41}, {"id": 42}]})
        if req.url.path == "/user/installations/42/repositories":
            assert req.headers["authorization"] == "Bearer ghu_user"  # the USER's view
            return httpx.Response(200, json={"repositories": [{"full_name": "org/mine"}]})
        if req.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404)

    app, seen = make(handler)
    access = await app.authorize("code", 42, keep_token=False)
    assert access is not None
    assert access.repos == ("org/mine",)
    assert access.secret is None
    assert await app.authorize("code", 99, keep_token=False) is None  # someone else's id
    assert sum(1 for r in seen if r.method == "DELETE") == 2  # revoked both times


async def test_kept_token_is_not_revoked_and_rechecks_later() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "ghu_user"})
        if req.url.path == "/user/installations":
            return httpx.Response(200, json={"installations": [{"id": 42}]})
        if req.url.path == "/user/installations/42/repositories":
            return httpx.Response(200, json={"repositories": [{"full_name": "org/mine"}]})
        return httpx.Response(404)

    app, seen = make(handler)
    access = await app.authorize("code", 42, keep_token=True)
    assert access is not None
    assert access.secret is not None
    assert "ghu_user" in access.secret
    assert not any(r.method == "DELETE" for r in seen)
    again = await app.recheck(access.secret, 42)
    assert again.repos == ("org/mine",)


async def test_recheck_reports_a_dead_token_as_unverifiable() -> None:
    app, _ = make(lambda r: httpx.Response(401, json={"message": "Bad credentials"}))
    result = await app.recheck('{"a": "ghu_dead", "r": null, "e": null}', 42)
    assert result.repos is None
    assert (await app.recheck("not json", 42)).repos is None


async def test_expired_token_is_refreshed_and_the_new_one_returned() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/login/oauth/access_token":
            assert b"grant_type=refresh_token" in req.content
            return httpx.Response(
                200,
                json={"access_token": "ghu_new", "refresh_token": "ghr_new", "expires_in": 28800},
            )
        if req.url.path == "/user/installations":
            assert req.headers["authorization"] == "Bearer ghu_new"
            return httpx.Response(200, json={"installations": [{"id": 42}]})
        return httpx.Response(200, json={"repositories": []})

    app, _ = make(handler)
    result = await app.recheck('{"a": "ghu_old", "r": "ghr_old", "e": 1}', 42)
    assert result.repos == ()
    assert "ghr_new" in result.secret  # GitHub rotates refresh tokens: store the new one


async def test_a_rejected_oauth_code_is_not_ownership() -> None:
    app, _ = make(lambda r: httpx.Response(200, json={"error": "bad_verification_code"}))
    assert await app.authorize("stale", 42, keep_token=True) is None
    assert await app.authorize("", 42, keep_token=True) is None


def _tarball(files: dict[str, bytes], *, symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, data in files.items():
            info = tarfile.TarInfo(f"o-r-abc123/{path}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if symlink:
            link = tarfile.TarInfo(f"o-r-abc123/{symlink}")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            tar.addfile(link)
    return buf.getvalue()


async def test_doc_files_keeps_docs_only_and_skips_links_and_huge_files() -> None:
    body = _tarball(
        {
            "README.md": b"# Hi",
            "docs/guide.md": b"guide",
            "src/app.py": b"code",
            "node_modules/x/README.md": b"vendored",
            "docs/huge.md": b"x" * (256 * 1024 + 1),
        },
        symlink="docs/evil.md",
    )
    app, _ = make(lambda r: httpx.Response(200, content=body))
    items = [f async for f in app.doc_files(1, "o/r", "main")]
    files = {f.path: f.data for f in items if isinstance(f, RepoFile)}
    assert files == {"README.md": b"# Hi", "docs/guide.md": b"guide"}
    assert items[-1] == TreeSkips(  # what was left out, and why (P0-4); links aren't files
        {"excluded_path": 1, "too_large:file": 1, "unsupported_type:code": 1}
    )


async def test_doc_files_also_yields_the_overview_tree_and_root_manifests() -> None:
    """P1-2: the same tarball walk feeds the overview document, at no extra download."""
    body = _tarball(
        {
            "README.md": b"# Hi",
            "package.json": b'{"name": "wave"}',
            "src/app.py": b"code",
            "src/deep/inner.py": b"code",
            "packages/ui/package.json": b"{}",
            "node_modules/x/index.js": b"vendored",
        }
    )
    app, _ = make(lambda r: httpx.Response(200, content=body))
    items = [f async for f in app.doc_files(1, "o/r", "main")]
    [tree] = [f for f in items if isinstance(f, TreeOverview)]
    assert set(tree.entries) == {"README.md", "package.json", "src/app.py"}
    assert tree.manifests == {"package.json": b'{"name": "wave"}'}  # root manifests only
    assert isinstance(items[-1], TreeSkips)


async def test_latest_release_is_read_and_none_when_there_is_none() -> None:
    release = {"tag_name": "v1.0", "name": "First", "body": "Notes.",
               "published_at": "2026-11-01T00:00:00Z"}  # fmt: skip
    app, seen = make(lambda r: httpx.Response(200, json=release))
    got = await app.latest_release(1, "o/r")
    assert got is not None
    assert (got.tag, got.name, got.body) == ("v1.0", "First", "Notes.")
    assert seen[-1].url.path == "/repos/o/r/releases/latest"
    app, _ = make(lambda r: httpx.Response(404))
    assert await app.latest_release(1, "o/r") is None


async def test_missing_repo_and_lost_access_are_typed() -> None:
    app, _ = make(lambda r: httpx.Response(404))
    with pytest.raises(NotFound):
        await app.repo(1, "o/r")
    app, _ = make(lambda r: httpx.Response(403, json={}))
    with pytest.raises(PermissionDenied):
        await app.repo(1, "o/r")


async def test_rate_limits_beyond_a_minute_are_raised_with_retry_after() -> None:
    app, _ = make(lambda r: httpx.Response(429, headers={"retry-after": "900"}))
    with pytest.raises(RateLimited) as info:
        await app.repo(1, "o/r")
    assert info.value.extra["retry_after_s"] == 900


async def test_issues_paginate_and_mark_prs() -> None:
    page1 = [{"number": 1, "title": "Bug", "body": "b", "state": "open", "html_url": "u1",
              "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z",
              "user": {"login": "ada"}, "labels": [{"name": "bug"}]}]  # fmt: skip
    page2 = [{"number": 2, "title": "PR", "body": None, "state": "closed", "html_url": "u2",
              "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-03T00:00:00Z",
              "pull_request": {"merged_at": "2026-01-03T00:00:00Z"}}]  # fmt: skip

    def handler(req: httpx.Request) -> httpx.Response:
        if "page=2" in str(req.url):
            return httpx.Response(200, json=page2)
        assert req.url.params["since"] == "2025-12-31T00:00:00Z"
        nxt = '<https://api.github.com/repos/o/r/issues?page=2>; rel="next"'
        return httpx.Response(200, json=page1, headers={"link": nxt})

    app, _ = make(handler)
    got = [i async for i in app.issues(1, "o/r", "2025-12-31T00:00:00Z", limit=10)]
    assert [(i.number, i.is_pr, i.merged) for i in got] == [(1, False, False), (2, True, True)]
    assert got[0].author == "ada"
    assert got[0].labels == ("bug",)


async def test_head_commit_asks_for_the_sha_and_keeps_auth() -> None:
    # Regression (P0-0): a caller's headers= collided with _get's own and raised TypeError,
    # which broke every GitHub sync before any file was read.
    app, seen = make(lambda r: httpx.Response(200, text="abc123\n"))
    assert await app.head_commit(1, "o/r", "main") == "abc123"
    req = seen[-1]
    assert req.url.path == "/repos/o/r/commits/main"
    assert req.headers["accept"] == "application/vnd.github.sha"  # the caller's override wins
    assert req.headers["authorization"] == "Bearer ghs_inst"  # but never over the token
    assert req.headers["x-github-api-version"] == "2022-11-28"


async def test_get_requests_carry_github_headers() -> None:
    repo = {"id": 1, "full_name": "o/r", "private": True, "default_branch": "main", "html_url": "u"}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/tarball/main"):
            return httpx.Response(200, content=_tarball({"README.md": b"# Hi"}))
        if req.url.path.endswith("/issues"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=repo)

    app, seen = make(handler)
    await app.repo(1, "o/r")
    _ = [i async for i in app.issues(1, "o/r", None, limit=10)]
    _ = [f async for f in app.doc_files(1, "o/r", "main")]
    calls = [r for r in seen if not r.url.path.endswith("/access_tokens")]
    assert len(calls) == 3
    for req in calls:
        assert req.headers["accept"] == "application/vnd.github+json"
        assert req.headers["x-github-api-version"] == "2022-11-28"
        assert req.headers["authorization"] == "Bearer ghs_inst"


async def test_invalid_repo_names_never_reach_github() -> None:
    app, seen = make(lambda r: httpx.Response(200))
    with pytest.raises(ValueError, match="invalid"):
        await app.repo(1, "../../etc")
    assert seen == []


async def test_pagination_never_sends_the_token_off_github() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "api.github.com"
        return httpx.Response(
            200, json={"repositories": []}, headers={"link": '<https://evil.example/x>; rel="next"'}
        )

    app, seen = make(handler)
    assert await app.installation_repos(1) == []
    assert all(r.url.host == "api.github.com" for r in seen)


async def test_a_tarball_with_too_many_entries_is_cut_off(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.adapters.github.app as mod

    monkeypatch.setattr(mod, "MAX_TAR_MEMBERS", 3)
    body = _tarball({f"docs/{i}.md": b"x" for i in range(10)})
    app, _ = make(lambda r: httpx.Response(200, content=body))
    items = [f async for f in app.doc_files(1, "o/r", "main")]
    assert len([f for f in items if isinstance(f, RepoFile)]) == 3
    assert items[-1] == TreeSkips({"cap_reached": 1})


async def test_doc_files_over_the_file_cap_are_counted_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.adapters.github.app as mod

    monkeypatch.setattr(mod, "MAX_DOC_FILES", 2)
    body = _tarball({f"docs/{i}.md": b"x" for i in range(5)})
    app, _ = make(lambda r: httpx.Response(200, content=body))
    items = [f async for f in app.doc_files(1, "o/r", "main")]
    assert len([f for f in items if isinstance(f, RepoFile)]) == 2
    assert items[-1] == TreeSkips({"cap_reached:file": 3})


@pytest.mark.parametrize(
    "mangle",
    [
        lambda p: p,
        lambda p: p.replace("\n", "\n"),  # literal \n in the env var
        lambda p: p.replace("\n", " "),  # newlines turned into spaces (the production bug)
        lambda p: f'"{p}"',  # pasted with quotes
        lambda p: base64.b64encode(p.encode()).decode(),  # whole file base64-encoded
    ],
    ids=["as-is", "escaped", "spaces", "quoted", "base64"],
)
def test_pasted_keys_in_any_common_shape_still_sign(mangle: Callable[[str], str]) -> None:
    token = app_jwt("123", mangle(PEM), now=1_000_000)
    head, body, sig = token.split(".")
    KEY.public_key().verify(
        _unb64(sig), f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )


async def test_a_broken_key_is_a_clear_error_not_a_500() -> None:
    from core.domain.errors import UpstreamUnavailable

    app = GitHubApp(
        app_id="1", slug="s", client_id="c", client_secret="x", private_key="-----BEGIN junk",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
    )  # fmt: skip
    with pytest.raises(UpstreamUnavailable, match="private key"):
        await app.repo(1, "o/r")
