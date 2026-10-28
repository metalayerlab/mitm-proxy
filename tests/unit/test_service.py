import socket

import pytest

from mitm_proxy.errors import StartupError
from mitm_proxy.service import bind


def test_bind_returns_a_listening_socket() -> None:
    sock = bind("127.0.0.1", 0)
    try:
        assert sock.getsockname()[1] > 0
    finally:
        sock.close()


def test_bind_failure_is_a_startup_error() -> None:
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    try:
        with pytest.raises(StartupError, match="cannot listen"):
            bind("127.0.0.1", holder.getsockname()[1])
    finally:
        holder.close()
