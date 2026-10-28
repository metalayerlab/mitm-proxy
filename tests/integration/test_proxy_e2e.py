"""The real thing over loopback sockets: client -> proxy listener -> verified TLS origin.

Opt-in (RUN_INTEGRATION=1). Nothing leaves 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from mitm_proxy.config import Config, load_config
from mitm_proxy.http.flow import Response
from mitm_proxy.http.h1 import H1Server, H1Upstream
from mitm_proxy.http.h2 import H2Server
from mitm_proxy.http.headers import Headers
from mitm_proxy.http.messages import IncomingRequest, Responder
from mitm_proxy.server import ProxyServer
from mitm_proxy.streams import PlainStream
from mitm_proxy.tls.terminate import TLSStream
from tests.harness import send
from tests.helpers import make_ca, trusting_client_context

pytestmark = pytest.mark.integration


async def origin_handler(incoming: IncomingRequest, responder: Responder) -> None:
    body = (
        b"".join([c async for c in incoming.body])
        or b"origin says hi via " + responder.protocol.encode()
    )
    headers = Headers([("content-type", "text/plain"), ("content-length", str(len(body)))])
    await responder.start(Response(200, headers), headers, end=False)
    await responder.data(body)
    await responder.end(None)


@contextlib.asynccontextmanager
async def tls_origin(ca_name: str = "origin CA") -> AsyncIterator[tuple[int, bytes]]:
    """A TLS origin on 127.0.0.1 speaking h2 or HTTP/1.1; yields (port, its CA's PEM)."""
    ca = make_ca(ca_name)
    ctx = ca.server_context("127.0.0.1")

    async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        stream = PlainStream(reader, writer)
        alpn = writer.get_extra_info("ssl_object").selected_alpn_protocol()
        if alpn == "h2":
            await H2Server(stream, origin_handler, idle_timeout=5).run()
        else:
            await H1Server(stream, origin_handler, idle_timeout=5).run()

    server = await asyncio.start_server(on_client, "127.0.0.1", 0, ssl=ctx)
    try:
        yield server.sockets[0].getsockname()[1], ca.cert_pem
    finally:
        server.close()
        ca.close()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def proxy_config(tmp_path: Path, extra_ca: bytes | None, **env: str) -> Config:
    environ = {
        "MITM_PROXY_STATE_DIR": str(tmp_path / "state"),
        "MITM_PROXY_PROXY_PORT": str(free_port()),
        "MITM_PROXY_INTERCEPT_HOSTS": "127.0.0.1",
        **env,
    }
    if extra_ca is not None:
        bundle = tmp_path / "origin-ca.pem"
        bundle.write_bytes(extra_ca)
        environ["MITM_PROXY_UPSTREAM_EXTRA_CA_FILE"] = str(bundle)
    return load_config(tmp_path / "none.yaml", environ=environ)


async def connect_through(proxy: ProxyServer, port: int, alpn: str) -> H1Upstream:
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
    raw = PlainStream(reader, writer)
    await raw.write(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
    head = await reader.readuntil(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200"), head
    tls = TLSStream(
        raw,
        trusting_client_context(proxy.ca, [alpn]),
        server_side=False,
        server_hostname="127.0.0.1",
    )
    await tls.handshake()
    return H1Upstream(tls, read_timeout=5)


async def test_intercepts_a_verified_tls_origin(tmp_path: Path) -> None:
    async with tls_origin() as (port, origin_ca):
        proxy = ProxyServer(proxy_config(tmp_path, origin_ca))
        await proxy.start()
        try:
            client = await connect_through(proxy, port, "http/1.1")
            reply = await send(client, authority=f"127.0.0.1:{port}")
            # The origin negotiated h2 with the proxy while the client spoke HTTP/1.1.
            assert (reply.status, reply.body) == (200, b"origin says hi via h2")
            echoed = await send(client, "POST", body=b"round trip", authority=f"127.0.0.1:{port}")
            assert echoed.body == b"round trip"
            await client.close()
        finally:
            await proxy.stop()


async def test_an_unverifiable_origin_is_a_502(tmp_path: Path) -> None:
    async with tls_origin() as (port, _):
        proxy = ProxyServer(proxy_config(tmp_path, None))  # the origin's CA is not trusted
        await proxy.start()
        try:
            client = await connect_through(proxy, port, "http/1.1")
            reply = await send(client, authority=f"127.0.0.1:{port}")
            assert reply.status == 502
            assert b"certificate verification failed" in reply.body
            await client.close()
        finally:
            await proxy.stop()


async def test_service_runs_until_sigterm(tmp_path: Path) -> None:
    from mitm_proxy import service

    config = proxy_config(tmp_path, None, MITM_PROXY_LISTEN_PORT=str(free_port()))
    started: list[ProxyServer] = []
    real_start = ProxyServer.start

    async def recording_start(self: ProxyServer) -> None:
        started.append(self)
        await real_start(self)

    ProxyServer.start = recording_start  # type: ignore[method-assign]  # observe the instance
    try:
        task = asyncio.create_task(service.run(config))
        while not started or started[0].state != "ready":
            await asyncio.sleep(0.01)
        proxy = started[0]
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        writer.write(b"GET http://127.0.0.1:9/ HTTP/1.1\r\nHost: 127.0.0.1:9\r\n\r\n")
        head = await reader.readuntil(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 502")  # nothing listens on port 9
        writer.close()
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, 5)
        assert proxy.state == "stopped"
    finally:
        ProxyServer.start = real_start  # type: ignore[method-assign]  # restore
