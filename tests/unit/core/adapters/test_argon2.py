from core.adapters.security.argon2_hasher import Argon2Hasher


def test_argon2_roundtrip_and_rejects_wrong_password() -> None:
    hasher = Argon2Hasher()
    h = hasher.hash("correct horse battery")
    assert h.startswith("$argon2id$")
    assert hasher.verify(h, "correct horse battery")
    assert not hasher.verify(h, "wrong")
    assert not hasher.verify("not-a-hash", "anything")
    assert not hasher.needs_rehash(h)


def test_argon2_salts_every_hash() -> None:
    hasher = Argon2Hasher()
    assert hasher.hash("same") != hasher.hash("same")
