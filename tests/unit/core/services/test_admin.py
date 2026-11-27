from __future__ import annotations

from datetime import date, timedelta

import pytest

from core.domain.admin import (
    AUTH_EVENT_RETENTION,
    AuthEventKind,
    Bucket,
    UserSort,
    UserStatus,
    fill_series,
)
from core.domain.auth import Identity, Role, hash_token
from core.domain.errors import NotFound, PermissionDenied, ValidationFailed
from core.domain.ids import SessionId, UserId, WorkspaceId
from core.domain.principals import Principals
from core.services.admin import AdminService, sweep_auth_events
from tests.fakes.admin import InMemoryAdminStore
from tests.fakes.auth import InMemoryAccounts, InMemoryAuthEvents, InMemorySessions
from tests.fakes.system import FakeClock

OWNER = "admin@slate.test"


class Env:
    def __init__(self) -> None:
        self.clock = FakeClock()
        self.accounts = InMemoryAccounts(clock=self.clock)
        self.sessions = InMemorySessions(self.accounts, self.clock)
        self.events = InMemoryAuthEvents(self.clock)
        self.svc = AdminService(
            InMemoryAdminStore(self.accounts, self.sessions, self.events),
            self.events, self.sessions, self.clock, owner_email=OWNER,
        )  # fmt: skip
        self.admin_id = self.accounts.add_user(OWNER, "Admin", "h", verified=True)

    def identity(self, uid: UserId, *, admin: bool = False, verified: bool = True) -> Identity:
        rec = self.accounts.users[uid]
        ws = WorkspaceId(self.accounts.ids.new_uuid())
        return Identity(
            user_id=uid, workspace_id=ws, session_id=SessionId(self.accounts.ids.new_uuid()),
            email=rec.email, name=rec.name, role=Role.OWNER,
            principals=Principals.for_user(uid, ws, []), csrf_secret="c",
            email_verified=verified, is_platform_admin=admin,
        )  # fmt: skip

    @property
    def admin(self) -> Identity:
        return self.identity(self.admin_id, admin=True)


@pytest.fixture
def env() -> Env:
    return Env()


async def test_every_method_hides_from_non_admins(env: Env) -> None:
    bob = env.identity(env.accounts.add_user("bob@acme.dev", "Bob", "h", verified=True))
    calls = [
        env.svc.overview(bob),
        env.svc.signups(bob, days=30),
        env.svc.failed_logins(bob),
        env.svc.users(bob),
        env.svc.events(bob),
        env.svc.set_disabled(bob, env.admin_id, disabled=True),
    ]
    for call in calls:
        with pytest.raises(NotFound):  # 404, not 403: the console doesn't reveal it exists
            await call


async def test_flag_without_verified_email_is_not_enough(env: Env) -> None:
    unverified = env.identity(env.admin_id, admin=True, verified=False)
    with pytest.raises(NotFound):
        await env.svc.overview(unverified)


async def test_overview_counts(env: Env) -> None:
    env.accounts.add_user("a@acme.dev", "A", "h", verified=True)
    old = env.accounts.add_user("b@acme.dev", "B", "h")
    env.accounts.created_at[old] = env.clock.now() - timedelta(days=10)
    env.accounts.disable(old)
    await env.events.record(AuthEventKind.LOGIN_FAILED, None)
    o = await env.svc.overview(env.admin)
    assert (o.total, o.verified, o.unverified, o.disabled) == (3, 2, 0, 1)
    assert (o.new_7d, o.new_prev_7d, o.new_30d) == (2, 1, 3)
    assert o.failed_logins_24h == 1


async def test_signup_series_has_no_gaps(env: Env) -> None:
    uid = env.accounts.add_user("a@acme.dev", "A", "h")
    env.accounts.created_at[uid] = env.clock.now() - timedelta(days=3)
    points = await env.svc.signups(env.admin, days=30)
    assert len(points) == 30
    assert sum(p.count for p in points) == 2
    assert points[-1].start == env.clock.now().date()


async def test_signup_range_is_validated(env: Env) -> None:
    with pytest.raises(ValidationFailed):
        await env.svc.signups(env.admin, days=7)


def test_fill_series_month_buckets_cross_year() -> None:
    pts = fill_series({date(2026, 12, 1): 4}, date(2026, 11, 15), date(2027, 1, 2), Bucket.MONTH)
    assert [(p.start, p.count) for p in pts] == [
        (date(2026, 11, 1), 0), (date(2026, 12, 1), 4), (date(2027, 1, 1), 0),
    ]  # fmt: skip


def test_fill_series_weeks_start_monday() -> None:
    pts = fill_series({}, date(2026, 11, 28), date(2026, 12, 8), Bucket.WEEK)  # a Saturday
    assert [p.start for p in pts] == [date(2026, 11, 23), date(2026, 11, 30), date(2026, 12, 7)]


async def test_users_filter_search_sort_and_paginate(env: Env) -> None:
    for i in range(5):
        env.clock.advance(timedelta(minutes=1))
        env.accounts.add_user(f"user{i}@acme.dev", f"User {i}", "h", verified=i % 2 == 0)
    page = await env.svc.users(env.admin, status=UserStatus.UNVERIFIED)
    assert {u.email for u in page.items} == {"user1@acme.dev", "user3@acme.dev"}
    page = await env.svc.users(env.admin, query="  USER3 ")
    assert [u.email for u in page.items] == ["user3@acme.dev"]
    first = await env.svc.users(env.admin, sort=UserSort.EMAIL, descending=False, limit=4)
    assert first.items[0].email == OWNER
    assert first.total == 6
    assert first.next_cursor == "4"
    rest = await env.svc.users(env.admin, sort=UserSort.EMAIL, descending=False, cursor="4")
    assert len(rest.items) == 2
    assert rest.next_cursor is None


async def test_bad_cursor_is_refused(env: Env) -> None:
    with pytest.raises(ValidationFailed):
        await env.svc.users(env.admin, cursor="1; DROP TABLE users")


async def test_last_active_comes_from_sessions_and_sign_ins(env: Env) -> None:
    uid = env.accounts.add_user("a@acme.dev", "A", "h", verified=True)
    await env.events.record(AuthEventKind.LOGIN_SUCCESS, uid)
    env.clock.advance(timedelta(days=40))
    page = await env.svc.users(env.admin, status=UserStatus.INACTIVE)
    assert uid in {u.id for u in page.items}
    await env.sessions.create(
        user_id=uid, token_hash=hash_token("t"), csrf_secret="c",
        ttl=timedelta(days=1), ip=None, user_agent=None,
    )  # fmt: skip
    page = await env.svc.users(env.admin, status=UserStatus.ACTIVE)
    assert uid in {u.id for u in page.items}


async def test_disable_signs_out_everywhere_and_is_recorded(env: Env) -> None:
    uid = env.accounts.add_user("a@acme.dev", "A", "h", verified=True)
    for t in ("t1", "t2"):
        await env.sessions.create(
            user_id=uid, token_hash=hash_token(t), csrf_secret="c",
            ttl=timedelta(days=1), ip=None, user_agent=None,
        )  # fmt: skip
    updated = await env.svc.set_disabled(env.admin, uid, disabled=True)
    assert updated.disabled
    assert not [r for r in env.sessions.rows.values() if r["user_id"] == uid]
    assert env.events.rows[-1][:3] == (AuthEventKind.ACCOUNT_DISABLED, uid, env.admin_id)
    again = await env.svc.set_disabled(env.admin, uid, disabled=True)  # idempotent
    assert again.disabled
    assert len(env.events.rows) == 1
    enabled = await env.svc.set_disabled(env.admin, uid, disabled=False)
    assert not enabled.disabled
    assert env.events.kinds()[-1] is AuthEventKind.ACCOUNT_ENABLED


async def test_cannot_disable_self_or_the_owner_account(env: Env) -> None:
    with pytest.raises(PermissionDenied, match="your own"):
        await env.svc.set_disabled(env.admin, env.admin_id, disabled=True)
    # A second account holding the owner address in another spelling (Gmail-style dots
    # aside, case differs) still can't be disabled.
    twin = env.accounts.add_user("Admin@Slate.test", "Twin", "h")
    with pytest.raises(PermissionDenied, match="platform admin"):
        await env.svc.set_disabled(env.admin, twin, disabled=True)


async def test_disable_unknown_account(env: Env) -> None:
    with pytest.raises(NotFound):
        await env.svc.set_disabled(env.admin, UserId(env.accounts.ids.new_uuid()), disabled=True)


async def test_events_page_newest_first_with_kind_filter(env: Env) -> None:
    uid = env.accounts.add_user("a@acme.dev", "A", "h")
    for kind in (AuthEventKind.SIGNUP, AuthEventKind.LOGIN_FAILED, AuthEventKind.LOGIN_SUCCESS):
        await env.events.record(kind, uid)
    page = await env.svc.events(env.admin, limit=2)
    assert [e.kind for e in page.items] == [AuthEventKind.LOGIN_SUCCESS, AuthEventKind.LOGIN_FAILED]
    assert page.items[0].user_email == "a@acme.dev"
    rest = await env.svc.events(env.admin, cursor=page.next_cursor)
    assert [e.kind for e in rest.items] == [AuthEventKind.SIGNUP]
    assert rest.next_cursor is None
    failed = await env.svc.events(env.admin, kind=AuthEventKind.LOGIN_FAILED)
    assert len(failed.items) == 1


async def test_event_retention_sweep(env: Env) -> None:
    await env.events.record(AuthEventKind.SIGNUP, None)
    env.clock.advance(AUTH_EVENT_RETENTION + timedelta(hours=1))
    await env.events.record(AuthEventKind.LOGOUT, None)
    assert await sweep_auth_events(env.events, env.clock) == 1
    assert env.events.kinds() == [AuthEventKind.LOGOUT]


async def test_status_filters_match_the_overview_partition(env: Env) -> None:
    gone = env.accounts.add_user("gone@acme.dev", "Gone", "h", verified=True)
    env.accounts.disable(gone)
    verified = await env.svc.users(env.admin, status=UserStatus.VERIFIED)
    assert gone not in {u.id for u in verified.items}
    o = await env.svc.overview(env.admin)
    assert verified.total == o.verified


async def test_never_active_accounts_sort_last_both_ways(env: Env) -> None:
    seen = env.accounts.add_user("seen@acme.dev", "Seen", "h")
    await env.events.record(AuthEventKind.LOGIN_SUCCESS, seen)
    for descending in (True, False):
        page = await env.svc.users(env.admin, sort=UserSort.LAST_ACTIVE, descending=descending)
        assert page.items[0].id == seen


# ------------------------------------------------------------------ delete account


async def test_delete_removes_account_and_frees_the_email(env: Env) -> None:
    from core.domain.ids import WorkspaceId
    from tests.fakes.auth import _Member

    uid = env.accounts.add_user("a@acme.dev", "A", "h", verified=True)
    ws = WorkspaceId(env.accounts.ids.new_uuid())
    env.accounts.members[uid] = _Member(ws, Role.OWNER)
    env.accounts.workspaces[ws] = "A's workspace"
    await env.svc.delete_user(env.admin, uid, confirm_email=" A@ACME.dev ")
    assert uid not in env.accounts.users
    assert ws not in env.accounts.workspaces
    assert await env.accounts.find_user("a@acme.dev") is None  # free to sign up again
    assert env.events.rows[-1][:3] == (AuthEventKind.ACCOUNT_DELETED, None, env.admin_id)


async def test_delete_needs_the_typed_email(env: Env) -> None:
    uid = env.accounts.add_user("a@acme.dev", "A", "h")
    with pytest.raises(ValidationFailed, match="confirm"):
        await env.svc.delete_user(env.admin, uid, confirm_email="b@acme.dev")
    assert uid in env.accounts.users


async def test_delete_refuses_self_owner_and_shared_workspaces(env: Env) -> None:
    from core.domain.errors import Conflict
    from core.domain.ids import WorkspaceId
    from tests.fakes.auth import _Member

    with pytest.raises(PermissionDenied, match="your own"):
        await env.svc.delete_user(env.admin, env.admin_id, confirm_email=OWNER)
    twin = env.accounts.add_user("Admin@Slate.test", "Twin", "h")
    with pytest.raises(PermissionDenied, match="platform admin"):
        await env.svc.delete_user(env.admin, twin, confirm_email="Admin@Slate.test")
    host = env.accounts.add_user("host@acme.dev", "H", "h")
    guest = env.accounts.add_user("guest@acme.dev", "G", "h")
    ws = WorkspaceId(env.accounts.ids.new_uuid())
    env.accounts.members[host] = _Member(ws, Role.OWNER)
    env.accounts.extra[guest] = {ws: Role.MEMBER}
    with pytest.raises(Conflict, match="other people"):
        await env.svc.delete_user(env.admin, host, confirm_email="host@acme.dev")
    assert host in env.accounts.users


async def test_non_admin_cannot_delete(env: Env) -> None:
    bob = env.identity(env.accounts.add_user("bob@acme.dev", "Bob", "h", verified=True))
    with pytest.raises(NotFound):
        await env.svc.delete_user(bob, env.admin_id, confirm_email=OWNER)
