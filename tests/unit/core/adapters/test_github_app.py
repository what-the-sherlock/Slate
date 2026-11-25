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
    assert await app.user_installation_repos("code", 42) == ["org/mine"]
    assert await app.user_installation_repos("code", 99) is None  # someone else's id
    assert sum(1 for r in seen if r.method == "DELETE") == 2  # revoked both times


async def test_a_rejected_oauth_code_is_not_ownership() -> None:
    app, _ = make(lambda r: httpx.Response(200, json={"error": "bad_verification_code"}))
    assert await app.user_installation_repos("stale", 42) is None
    assert await app.user_installation_repos("", 42) is None


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
    files = {f.path: f.data async for f in app.doc_files(1, "o/r", "main")}
    assert files == {"README.md": b"# Hi", "docs/guide.md": b"guide"}


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
    assert len([f async for f in app.doc_files(1, "o/r", "main")]) == 3


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
