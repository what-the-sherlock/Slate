from uuid import uuid4

from hypothesis import given
from hypothesis import strategies as st

from core.domain.ids import GroupId, UserId, WorkspaceId
from core.domain.principals import PUBLIC, Principals, is_valid_principal


def test_principals_member_gets_user_workspace_groups_and_public() -> None:
    u, w, g = UserId(uuid4()), WorkspaceId(uuid4()), GroupId(uuid4())
    p = Principals.for_user(u, w, [g])
    assert set(p.values) == {f"user:{u}", f"workspace:{w}", f"group:{g}", PUBLIC}


def test_an_outsider_gets_only_self_and_public() -> None:
    u = UserId(uuid4())
    p = Principals.for_outsider(u)
    assert set(p.values) == {f"user:{u}", PUBLIC}
    assert not any(v.startswith(("workspace:", "group:")) for v in p.values)


def test_principals_are_sorted_and_deduplicated() -> None:
    u, w, g = UserId(uuid4()), WorkspaceId(uuid4()), GroupId(uuid4())
    p = Principals.for_user(u, w, [g, g])
    assert list(p.values) == sorted(set(p.values))


def test_is_valid_principal() -> None:
    assert is_valid_principal("public")
    assert is_valid_principal(f"user:{uuid4()}")
    assert not is_valid_principal("user:not-a-uuid")
    assert not is_valid_principal("admin:" + str(uuid4()))
    assert not is_valid_principal("public:x")


@given(st.text(max_size=40))
def test_is_valid_principal_never_raises(value: str) -> None:
    assert isinstance(is_valid_principal(value), bool)
