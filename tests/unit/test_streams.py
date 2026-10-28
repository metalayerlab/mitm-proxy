import asyncio
import socket
import ssl

import pytest

from mitm_proxy.relay import relay
from mitm_proxy.streams import PlainStream, prepend
from mitm_proxy.tls.terminate import TLSStream
from tests.helpers import make_ca, memory_pair, trusting_client_context


async def test_tls_stream_round_trip_with_replayed_prefix() -> None:
    ca = make_ca()
    client_raw, server_raw = memory_pair()
    client = TLSStream(
        client_raw,
        trusting_client_context(ca, ["h2", "http/1.1"]),
        server_side=False,
        server_hostname="example.org",
    )

    async def server_side() -> bytes:
        # Peek the first flight as the proxy does, then replay it into the server.
        first = await server_raw.read()
        server = TLSStream(
            server_raw, ca.server_context("example.org"), server_side=True, initial=first
        )
        await server.handshake()
        assert server.alpn == "h2"
        assert server.version is not None and server.cipher is not None
        data = await server.read()
        await server.write(data.upper())
        await server.close()
        return data

    task = asyncio.create_task(server_side())
    await client.handshake()
    assert client.alpn == "h2"
    await client.write(b"hello")
    assert await client.read() == b"HELLO"
    assert await client.read() == b""
    assert await task == b"hello"
    assert client.is_closing() is False
    await client.close()
    await client.close()
    assert client.is_closing()
    with pytest.raises(ConnectionResetError):
        await client.write(b"late")
    client.write_eof()


async def test_untrusted_leaf_fails_the_handshake() -> None:
    ca, other = make_ca(), make_ca("other")
    client_raw, server_raw = memory_pair()
    client = TLSStream(
        client_raw, trusting_client_context(other), server_side=False, server_hostname="example.org"
    )
    server = TLSStream(server_raw, ca.server_context("example.org"), server_side=True)
    server_task = asyncio.create_task(server.handshake())
    with pytest.raises(ssl.SSLCertVerificationError):
        await client.handshake()
    with pytest.raises((ssl.SSLError, ConnectionError)):
        await server_task


async def test_handshake_eof() -> None:
    ca = make_ca()
    client_raw, server_raw = memory_pair()
    server = TLSStream(server_raw, ca.server_context("example.org"), server_side=True)
    await client_raw.close()
    with pytest.raises(ConnectionResetError):
        await server.handshake()


async def test_relay_copies_both_ways_and_passes_half_close() -> None:
    client, proxy_client_side = memory_pair()
    proxy_upstream_side, origin = memory_pair()
    task = asyncio.create_task(relay(proxy_client_side, proxy_upstream_side, idle_timeout=5))
    await client.write(b"ping")
    assert await origin.read() == b"ping"
    await origin.write(b"pong!")
    assert await client.read() == b"pong!"
    client.write_eof()
    assert await origin.read() == b""  # half-close passed on
    await origin.write(b"bye")
    assert await client.read() == b"bye"
    origin.write_eof()
    assert await task == (4, 8)


async def test_relay_idle_timeout_closes_both() -> None:
    _client, a = memory_pair()
    b, _origin = memory_pair()
    assert await relay(a, b, idle_timeout=0.05) == (0, 0)
    assert a.is_closing() and b.is_closing()


async def test_relay_outer_cancellation_propagates() -> None:
    _client, a = memory_pair()
    b, _origin = memory_pair()
    task = asyncio.create_task(relay(a, b, idle_timeout=5))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_relay_survives_a_reset() -> None:
    client, a = memory_pair()
    b, origin = memory_pair()
    await origin.close()
    task = asyncio.create_task(relay(a, b, idle_timeout=5))
    await client.write(b"x")
    client.write_eof()
    await asyncio.wait_for(task, 1)


async def test_plain_stream_over_a_socketpair() -> None:
    left, right = socket.socketpair()
    r1, w1 = await asyncio.open_connection(sock=left)
    r2, w2 = await asyncio.open_connection(sock=right)
    one, two = PlainStream(r1, w1, prefix=b"pre"), PlainStream(r2, w2)
    assert await one.read(2) == b"pr"
    assert await one.read() == b"e"
    await two.write(b"data")
    assert await one.read() == b"data"
    assert isinstance(one.peer(), tuple)
    two.write_eof()
    assert await one.read() == b""
    assert one.is_closing()
    await one.close()
    await two.close()


async def test_prepend() -> None:
    a, b = memory_pair()
    assert prepend(a, b"") is a
    p = prepend(a, b"xy")
    await b.write(b"z")
    assert [await p.read(1), await p.read(), await p.read()] == [b"x", b"y", b"z"]
    await p.write(b"out")
    assert await b.read() == b"out"
    p.write_eof()
    assert await b.read() == b""
    assert not p.is_closing()
    await p.close()
    assert p.is_closing()
