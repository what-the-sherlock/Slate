"""PasswordHasher over Argon2id (argon2-cffi): the OWASP-recommended password hash.

Argon2id is deliberately slow and memory-hard, so stolen hashes are expensive to
brute-force on GPUs. Parameters are argon2-cffi's RFC 9106 "low memory" defaults.
"""

from __future__ import annotations

from argon2 import PasswordHasher as _Argon2
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


class Argon2Hasher:
    def __init__(self) -> None:
        self._ph = _Argon2()

    def hash(self, password: str) -> str:
        return self._ph.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        try:
            return self._ph.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, password_hash: str) -> bool:
        return self._ph.check_needs_rehash(password_hash)
