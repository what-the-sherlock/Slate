"""Typed identifiers.

``NewType`` makes ``DocumentId`` and ``SourceId`` distinct to the type checker even
though both are UUIDs at runtime, so passing one where the other is expected is a
pyright error rather than a silent data bug.
"""

from __future__ import annotations

from typing import NewType
from uuid import UUID

WorkspaceId = NewType("WorkspaceId", UUID)
UserId = NewType("UserId", UUID)
GroupId = NewType("GroupId", UUID)
SourceId = NewType("SourceId", UUID)
DocumentId = NewType("DocumentId", UUID)
JobId = NewType("JobId", UUID)
SessionId = NewType("SessionId", UUID)
ChatSessionId = NewType("ChatSessionId", UUID)
MessageId = NewType("MessageId", UUID)
RunId = NewType("RunId", UUID)

ChunkId = NewType("ChunkId", int)  # bigserial
GenerationId = NewType("GenerationId", int)  # smallserial
