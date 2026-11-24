import socket

import pytest
from pytest_socket import SocketConnectBlockedError


def test_packages_import() -> None:
    import api  # noqa: F401
    import core  # noqa: F401
    import worker  # noqa: F401


@pytest.mark.filterwarnings("ignore:A test tried to use socket")
def test_unit_tests_cannot_open_network_sockets() -> None:
    # pytest-socket enforces the "unit tests never touch the network" rule.
    with pytest.raises(SocketConnectBlockedError):
        socket.create_connection(("93.184.216.34", 80), timeout=1)
