"""Tokenizer over tiktoken's cl100k_base (ARCHITECTURE §7: ~512-token chunks).

The encoding file is downloaded and cached on first use, so this is only constructed in
the worker and the API composition roots, never in unit tests (which use a fake).
"""

from __future__ import annotations

import tiktoken


class TiktokenTokenizer:
    def __init__(self, encoding: str = "cl100k_base") -> None:
        self._enc = tiktoken.get_encoding(encoding)

    def count(self, text: str) -> int:
        return len(self._enc.encode(text, disallowed_special=()))
