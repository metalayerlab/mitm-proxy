"""The process: the proxy listener and the admin API on one event loop, until a signal.

uvicorn's own signal handling is switched off so that a SIGTERM shuts things down
in order: readiness goes 503, the admin API stops, then the proxy drains its
connections for ``proxy.shutdown_grace_ms``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import socket
from collections.abc import Iterator

import uvicorn

from mitm_proxy.api import create_app
from mitm_proxy.config import Config
from mitm_proxy.errors import StartupError
from mitm_proxy.server import ProxyServer

log = logging.getLogger(__name__)


class _AdminServer(uvicorn.Server):
    """uvicorn, minus its own signal handling: the proxy shuts both servers down in order."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def bind(host: str, port: int) -> socket.socket:
    """A listening socket for the admin API, so a bind failure is ours to report."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
        family, kind, proto, _, address = infos[0]
        sock = socket.socket(family, kind, proto)
    except OSError as e:
        raise StartupError(f"cannot listen on {host}:{port}: {e.strerror or e}") from e
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(address)
        sock.listen(128)
        sock.setblocking(False)
    except OSError as e:
        sock.close()
        raise StartupError(f"cannot listen on {host}:{port}: {e.strerror or e}") from e
    return sock


async def run(config: Config) -> None:
    """Run until SIGTERM/SIGINT: the proxy listener and the admin API."""
    proxy = ProxyServer(config)
    try:
        admin_socket = bind(config.host, config.port)
        try:
            await proxy.start()
            await _serve(config, proxy, admin_socket)
        finally:
            admin_socket.close()
    finally:
        await proxy.stop()


async def _serve(config: Config, proxy: ProxyServer, admin_socket: socket.socket) -> None:
    admin = _AdminServer(
        uvicorn.Config(
            create_app(config, proxy),
            log_level=config.log_level.lower(),
            proxy_headers=True,
            lifespan="off",
        )
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    api_task = asyncio.create_task(admin.serve(sockets=[admin_socket]))
    stop_task = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({api_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        log.info("shutting down")
        proxy.state = "stopping"
        admin.should_exit = True
        stop_task.cancel()
        await asyncio.gather(api_task, stop_task, return_exceptions=True)
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(sig)
