"""Chat history: rename, delete with Undo, and the purge (ADR-0044).

Delete is soft: the chat disappears from every read at once (the store's ownership
filter), can be restored for ``UNDO_WINDOW``, and the worker's hourly housekeeping then
deletes it for good, with its messages, citations and feedback. Every action is scoped
to the signed-in user's own chats: someone else's id is a plain 404, like a missing one.
"""

from __future__ import annotations

from datetime import timedelta

import structlog

from core.domain.auth import Identity
from core.domain.chat import clean_title
from core.domain.errors import NotFound
from core.domain.ids import ChatSessionId
from core.ports.chat import ChatStore
from core.ports.system import Clock

log = structlog.get_logger(__name__)

UNDO_WINDOW = timedelta(hours=24)
NOT_FOUND = "This chat doesn't exist."


class ChatHistory:
    def __init__(self, chats: ChatStore, clock: Clock) -> None:
        self._chats = chats
        self._clock = clock

    async def rename(self, identity: Identity, session: ChatSessionId, title: str) -> str:
        clean = clean_title(title)  # ValidationFailed for empty or control-only titles
        if not await self._chats.rename_session(
            identity.workspace_id, identity.user_id, session, clean
        ):
            raise NotFound(NOT_FOUND)
        return clean

    async def delete(self, identity: Identity, session: ChatSessionId) -> None:
        if not await self._chats.delete_session(identity.workspace_id, identity.user_id, session):
            raise NotFound(NOT_FOUND)
        log.info("chat.deleted")  # never the title: it's the user's words

    async def restore(self, identity: Identity, session: ChatSessionId) -> None:
        since = self._clock.now() - UNDO_WINDOW
        if not await self._chats.restore_session(
            identity.workspace_id, identity.user_id, session, since=since
        ):
            raise NotFound("This chat can't be restored any more.")


async def purge_deleted_chats(chats: ChatStore, clock: Clock) -> int:
    """Hourly: chats deleted more than ``UNDO_WINDOW`` ago are removed for good."""
    return await chats.purge_deleted(clock.now() - UNDO_WINDOW)
