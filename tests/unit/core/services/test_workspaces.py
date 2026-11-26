"""WorkspaceService: invites bound to an address, roles, switching, leaving, sharing."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from core.domain.auth import Identity, Role
from core.domain.data_class import DataClass
from core.domain.errors import Conflict, NotFound, PermissionDenied, RateLimited, ValidationFailed
from core.domain.ids import UserId
from core.domain.principals import Principals, user_principal, workspace_principal
from core.domain.workspaces import may_change_role, may_invite, may_remove
from core.ports.auth import SessionRecord
from core.services.workspaces import WorkspaceService
from tests.fakes.auth import FakeMailer, InMemoryAccounts, InMemorySessions
from tests.fakes.rate_limit import InMemoryRateLimiter
from tests.fakes.sources import InMemorySources
from tests.fakes.system import FakeClock
from tests.fakes.workspaces import InMemoryWorkspaces


class Env:
    def __init__(self) -> None:
        self.clock = FakeClock()
        self.accounts = InMemoryAccounts(clock=self.clock)
        self.sessions = InMemorySessions(self.accounts, self.clock)
        self.sources = InMemorySources()
        self.store = InMemoryWorkspaces(self.accounts, self.sources, self.clock)
        self.mailer = FakeMailer()
        self.svc = WorkspaceService(
            self.store, self.sessions, self.sources, InMemoryRateLimiter(self.clock), self.clock,
            mailer=self.mailer, app_url="https://slate.test",
        )  # fmt: skip
        self._n = 0

    async def account(self, email: str) -> Identity:
        _ws, uid = await self.accounts.create_user_with_workspace(
            email=email, email_canonical=email, name=email.split("@")[0], password_hash="h",
            workspace_name=f"{email} ws", slug=email,
        )  # fmt: skip
        await self.accounts.mark_email_verified(uid)
        self._n += 1
        rec = await self.sessions.create(
            user_id=uid, token_hash=bytes([self._n]) * 32, csrf_secret="c",
            ttl=timedelta(days=1), ip=None, user_agent=None,
        )  # fmt: skip
        assert rec is not None
        return _identity(rec)

    async def refresh(self, who: Identity) -> Identity:
        """Re-resolve the session, as the next request would."""
        token = next(
            r["token_hash"] for sid, r in self.sessions.rows.items() if sid == who.session_id
        )
        rec = await self.sessions.get(token)  # type: ignore[arg-type]
        assert rec is not None
        return _identity(rec)

    def token_from_mail(self) -> str:
        return self.mailer.sent[-1].text.split("/invite#", 1)[1].split()[0]


def _identity(rec: SessionRecord) -> Identity:
    return Identity(
        user_id=rec.user_id, workspace_id=rec.workspace_id, session_id=rec.id, email=rec.email,
        name=rec.name, role=rec.role, principals=Principals.for_user(rec.user_id, rec.workspace_id),
        csrf_secret=rec.csrf_secret, email_verified=rec.email_verified,
    )  # fmt: skip


@pytest.fixture
def env() -> Env:
    return Env()


async def test_invite_accept_switch_and_roles(env: Env) -> None:
    owner = await env.account("owner@acme.test")
    bob = await env.account("bob@acme.test")
    done = await env.svc.invite(owner, email="Bob@acme.test", role=Role.MEMBER)
    assert done.emailed
    assert done.link.startswith("https://slate.test/invite#")
    ws = await env.svc.accept(bob, env.token_from_mail())
    assert ws == owner.workspace_id
    bob = await env.refresh(bob)
    assert bob.workspace_id == owner.workspace_id  # switched on accept
    assert bob.role is Role.MEMBER
    spaces = {w.workspace_id for w in await env.svc.workspaces(bob)}
    assert owner.workspace_id in spaces
    assert len(spaces) == 2  # their own workspace plus the one they joined
    await env.svc.set_role(owner, bob.user_id, Role.ADMIN)
    assert (await env.refresh(bob)).role is Role.ADMIN


async def test_an_invite_only_works_for_the_invited_address(env: Env) -> None:
    owner = await env.account("owner@acme.test")
    mallory = await env.account("mallory@evil.test")
    await env.svc.invite(owner, email="bob@acme.test", role=Role.MEMBER)
    with pytest.raises(PermissionDenied, match="different email"):
        await env.svc.accept(mallory, env.token_from_mail())
    assert (await env.refresh(mallory)).workspace_id != owner.workspace_id


async def test_invites_are_single_use_revocable_and_expire(env: Env) -> None:
    owner = await env.account("owner@acme.test")
    bob = await env.account("bob@acme.test")
    first = await env.svc.invite(owner, email="bob@acme.test", role=Role.MEMBER)
    token1 = env.token_from_mail()
    await env.svc.invite(owner, email="bob@acme.test", role=Role.VIEWER)  # resend replaces
    with pytest.raises(ValidationFailed):
        await env.svc.accept(bob, token1)
    token2 = env.token_from_mail()
    await env.svc.revoke(owner, (await env.svc.invites(owner))[0].id)
    with pytest.raises(ValidationFailed):
        await env.svc.accept(bob, token2)
    await env.svc.invite(owner, email="bob@acme.test", role=Role.MEMBER)
    env.clock.advance(timedelta(days=15))
    with pytest.raises(ValidationFailed):
        await env.svc.accept(bob, env.token_from_mail())
    assert first.id


async def test_garbage_tokens_are_rejected_without_lookup(env: Env) -> None:
    bob = await env.account("bob@acme.test")
    for token in ("", "short", "x" * 200, "has spaces in it plenty long"):
        with pytest.raises(ValidationFailed):
            await env.svc.accept(bob, token)


async def test_role_rules(env: Env) -> None:
    owner = await env.account("owner@acme.test")
    with pytest.raises(PermissionDenied):
        await env.svc.invite(owner, email="x@acme.test", role=Role.OWNER)
    with pytest.raises(ValidationFailed):
        await env.svc.invite(owner, email=owner.email, role=Role.MEMBER)
    assert may_invite(Role.OWNER, Role.ADMIN)
    assert not may_invite(Role.ADMIN, Role.ADMIN)
    assert not may_invite(Role.MEMBER, Role.VIEWER)
    assert not may_change_role(Role.OWNER, Role.MEMBER, Role.OWNER, self_change=False)
    assert not may_change_role(Role.OWNER, Role.OWNER, Role.MEMBER, self_change=True)
    assert not may_remove(Role.ADMIN, Role.ADMIN, self_remove=False)
    assert may_remove(Role.MEMBER, Role.MEMBER, self_remove=True)
    assert not may_remove(Role.OWNER, Role.OWNER, self_remove=True)


async def test_member_invite_limits(env: Env) -> None:
    owner = await env.account("owner@acme.test")
    bob = await env.account("bob@acme.test")
    await env.svc.invite(owner, email="bob@acme.test", role=Role.MEMBER)
    await env.svc.accept(bob, env.token_from_mail())
    with pytest.raises(Conflict):
        await env.svc.invite(owner, email="bob@acme.test", role=Role.MEMBER)
    for i in range(19):
        await env.svc.invite(owner, email=f"p{i}@acme.test", role=Role.VIEWER)
    with pytest.raises(RateLimited):
        await env.svc.invite(owner, email="late@acme.test", role=Role.VIEWER)


async def test_switching_only_to_your_workspaces(env: Env) -> None:
    owner = await env.account("owner@acme.test")
    other = await env.account("other@acme.test")
    with pytest.raises(NotFound):
        await env.svc.switch(owner, other.workspace_id)


async def test_leaving_deletes_what_you_connected_and_falls_back(env: Env) -> None:
    owner = await env.account("owner@acme.test")
    bob = await env.account("bob@acme.test")
    await env.svc.invite(owner, email="bob@acme.test", role=Role.MEMBER)
    await env.svc.accept(bob, env.token_from_mail())
    bob = await env.refresh(bob)
    mine = await env.sources.create_source(
        owner.workspace_id, connector="github", name="bob/repo", config={},
        visibility=[user_principal(bob.user_id)], data_class=DataClass.PRIVATE,
        created_by=bob.user_id,
    )  # fmt: skip
    theirs = await env.sources.create_source(
        owner.workspace_id, connector="github", name="owner/repo", config={},
        visibility=[user_principal(owner.user_id)], data_class=DataClass.PRIVATE,
        created_by=owner.user_id,
    )  # fmt: skip
    assert await env.svc.remove(bob, bob.user_id) == 1
    assert mine not in env.sources.records
    assert theirs in env.sources.records
    after = await env.refresh(bob)
    assert after.workspace_id != owner.workspace_id  # back in their own workspace, at once


async def test_owner_cannot_leave_and_admin_cannot_remove_admin(env: Env) -> None:
    owner = await env.account("owner@acme.test")
    with pytest.raises(PermissionDenied, match="can't leave"):
        await env.svc.remove(owner, owner.user_id)
    with pytest.raises(NotFound):
        await env.svc.remove(owner, UserId(uuid4()))


async def test_only_the_connector_shares_a_source(env: Env) -> None:
    owner = await env.account("owner@acme.test")
    bob = await env.account("bob@acme.test")
    await env.svc.invite(owner, email="bob@acme.test", role=Role.ADMIN)
    await env.svc.accept(bob, env.token_from_mail())
    bob = await env.refresh(bob)
    sid = await env.sources.create_source(
        owner.workspace_id, connector="discord", name="Acme", config={},
        visibility=[user_principal(owner.user_id)], data_class=DataClass.PRIVATE,
        created_by=owner.user_id,
    )  # fmt: skip
    with pytest.raises(NotFound):  # an admin can't share someone else's source
        await env.svc.share(bob, sid, shared=True)
    await env.svc.share(owner, sid, shared=True)
    assert workspace_principal(owner.workspace_id) in env.sources.records[sid].visibility
    await env.svc.share(owner, sid, shared=False)
    assert env.sources.records[sid].visibility == (user_principal(owner.user_id),)
    assert [a for _, a, _ in env.store.audit_log if a.startswith("source.")] == [
        "source.shared", "source.unshared",
    ]  # fmt: skip


async def test_invite_without_mail_returns_a_link_to_share(env: Env) -> None:
    owner = await env.account("owner@acme.test")
    svc = WorkspaceService(
        env.store, env.sessions, env.sources, InMemoryRateLimiter(env.clock), env.clock,
        mailer=None, app_url=None,
    )  # fmt: skip
    done = await svc.invite(owner, email="bob@acme.test", role=Role.MEMBER)
    assert not done.emailed
    assert done.link.startswith("/invite#")
