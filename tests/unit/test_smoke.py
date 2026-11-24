import socket

import pytest
from pytest_socket import SocketBlockedError


def test_packages_import() -> None:
    import api  # noqa: F401
    import core  # noqa: F401
    import worker  # noqa: F401


def test_unit_tests_cannot_open_network_sockets() -> None:
    # pytest-socket enforces the "unit tests never touch the network" rule.
    with pytest.raises(SocketBlockedError):
        socket.create_connection(("example.com", 80), timeout=1)
