"""Data classification and the rule for which AI providers may see what (ADR-0012).

Private data may only go to providers under a no-training paid agreement (OpenAI API).
Free tiers (Gemini, Groq, Cloudflare Workers AI) may only receive data that is public:
readable by anyone in the world already. The rule fails closed: anything unknown is
treated as private.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class DataClass(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"


class ProviderTier(StrEnum):
    TRUSTED = "trusted"  # paid API, no training on inputs: may see private data
    FREE = "free"  # free tier, may train or retain: public data only


def classify(value: str | None) -> DataClass:
    """Parse a stored data class. Anything that isn't exactly 'public' is private."""
    return DataClass.PUBLIC if value == DataClass.PUBLIC.value else DataClass.PRIVATE


def strictest(classes: Iterable[DataClass | str | None]) -> DataClass:
    """The data class of a combined input: private if any part is (or if it's empty).

    An empty input is private because "we don't know what's in it" must never mean
    "safe to share".
    """
    seen = False
    for c in classes:
        seen = True
        if classify(c if not isinstance(c, DataClass) else c.value) is DataClass.PRIVATE:
            return DataClass.PRIVATE
    return DataClass.PUBLIC if seen else DataClass.PRIVATE


def may_send(tier: ProviderTier, classes: Iterable[DataClass | str | None]) -> bool:
    """Whether a call whose inputs have these classes may go to a provider of ``tier``.

    Callers pass the classes of *everything* in the request: retrieved chunks, earlier
    conversation turns, and the user's question (a question about private data is
    itself private).
    """
    if tier is ProviderTier.TRUSTED:
        return True
    return strictest(classes) is DataClass.PUBLIC
