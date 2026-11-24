"""Access-control principals (ARCHITECTURE §5).

A principal is a string: ``user:<uuid>``, ``group:<uuid>``, ``workspace:<uuid>`` or
``public``. Documents and chunks carry an ``acl`` list of principals; a user may see a
row when the two sets overlap (``acl && :principals`` in SQL).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from core.domain.ids import GroupId, UserId, WorkspaceId

PUBLIC = "public"


def user_principal(user_id: UserId) -> str:
    return f"user:{user_id}"


def group_principal(group_id: GroupId) -> str:
    return f"group:{group_id}"


def workspace_principal(workspace_id: WorkspaceId) -> str:
    return f"workspace:{workspace_id}"


def is_valid_principal(value: str) -> bool:
    if value == PUBLIC:
        return True
    kind, sep, raw = value.partition(":")
    if not sep or kind not in {"user", "group", "workspace"}:
        return False
    try:
        UUID(raw)
    except ValueError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class Principals:
    """The resolved set of principals for one request. Immutable and sorted."""

    values: tuple[str, ...]

    @classmethod
    def for_user(
        cls,
        user_id: UserId,
        workspace_id: WorkspaceId,
        group_ids: Iterable[GroupId] = (),
        *,
        is_guest: bool = False,
    ) -> Principals:
        """Guests get only their own principal and ``public`` (SECURITY.md)."""
        found = {user_principal(user_id), PUBLIC}
        if not is_guest:
            found.add(workspace_principal(workspace_id))
            found.update(group_principal(g) for g in group_ids)
        return cls(tuple(sorted(found)))

    def as_list(self) -> list[str]:
        return list(self.values)
