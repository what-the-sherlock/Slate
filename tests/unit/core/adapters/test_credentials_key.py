from cryptography.fernet import Fernet

from core.adapters.postgres.credentials import make_fernet


def test_make_fernet_handles_missing_bad_and_multiple_keys() -> None:
    assert make_fernet(None) is None
    assert make_fernet("  ") is None
    assert make_fernet("not-a-key") is None
    a, b = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    f = make_fernet(f"{a}, {b}")
    assert f is not None
    assert Fernet(b.encode()).decrypt(Fernet(b.encode()).encrypt(b"x")) == b"x"
    assert f.decrypt(Fernet(b.encode()).encrypt(b"y")) == b"y"  # any listed key decrypts
