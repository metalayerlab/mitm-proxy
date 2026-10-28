"""Edge cases of the client-facing HTTP/1.1 and HTTP/2 servers."""

import asyncio
import base64

import h2.config
import h2.connection
import h2.events

from mitm_proxy.http.h1 import H1Server, ProxyMode
from mitm_proxy.interception.base import InterceptedConnection
from mitm_proxy.metrics import Metrics
from tests.harness import Origin, bench
from tests.helpers import MemoryStream, memory_pair


async def read_until(stream: MemoryStream, marker: bytes) -> bytes:
    data = b""
    while marker not in data:
        chunk = await stream.read()
        if not chunk:
            break
        data += chunk
    return data


async def test_malformed_request_gets_400() -> None:
    b = bench()
    client = b.proxy(b.context("http/1.1"))
    await client.write(b"NOT HTTP AT ALL\r\n\r\n")
    assert (await read_until(client, b"\r\n\r\n")).startswith(b"HTTP/1.1 400")
    await b.close()


async def test_expect_100_continue_is_answered() -> None:
    b = bench(Origin(echo=True))
    client = b.proxy(
        b.context("http/1.1", [("watch", __import__("mitm_proxy.pipeline").pipeline.Middleware())])
    )
    await client.write(
        b"POST / HTTP/1.1\r\nHost: example.org\r\nContent-Length: 2\r\nExpect: 100-continue\r\n\r\n"
    )
    assert (await read_until(client, b"\r\n\r\n")).startswith(b"HTTP/1.1 100")
    await client.write(b"ok")
    assert (await read_until(client, b"ok")).startswith(b"HTTP/1.1 200")
    await b.close()


async def test_pipelined_requests_are_served_in_order() -> None:
    b = bench(Origin())
    client = b.proxy(b.context("http/1.1"))
    request = b"GET /x HTTP/1.1\r\nHost: example.org\r\n\r\n"
    await client.write(request * 2)
    data = await read_until(client, b"origin")
    data += await read_until(client, b"origin")
    assert data.count(b"HTTP/1.1 200") == 2
    await b.close()


async def test_idle_client_is_closed() -> None:
    client, proxy_end = memory_pair()

    async def never(*args: object) -> None:
        raise AssertionError("no request expected")

    await asyncio.wait_for(H1Server(proxy_end, never, idle_timeout=0.05).run(), 1)
    assert await client.read() == b""


def proxy_mode(auth: str = "", tunnels: list[InterceptedConnection] | None = None) -> ProxyMode:
    async def on_connect(conn: InterceptedConnection) -> None:
        if tunnels is not None:
            tunnels.append(conn)
        await conn.accept()

    metrics = Metrics("test")
    return ProxyMode(
        auth=auth, on_connect=on_connect, on_auth_failure=metrics.proxy_auth_failures.inc
    )


async def test_proxy_auth_407_then_success() -> None:
    b = bench(Origin())
    mode = proxy_mode("user:pass")
    client = b.proxy(b.context("http/1.1", target=None), mode)
    await client.write(b"GET http://example.org/ HTTP/1.1\r\nHost: example.org\r\n\r\n")
    head = await read_until(client, b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 407") and b'Basic realm="mitm-proxy"' in head
    token = base64.b64encode(b"user:pass")
    await client.write(
        b"GET http://example.org/ HTTP/1.1\r\nHost: example.org\r\nProxy-Authorization: Basic "
        + token
        + b"\r\n\r\n"
    )
    assert (await read_until(client, b"origin")).startswith(b"HTTP/1.1 200")
    assert "proxy-authorization" not in b.origin.seen[0].raw_headers
    await b.close()


async def test_proxy_mode_wants_absolute_http_urls() -> None:
    b = bench()
    client = b.proxy(b.context("http/1.1", target=None), proxy_mode())
    await client.write(b"GET /relative HTTP/1.1\r\nHost: example.org\r\n\r\n")
    assert (await read_until(client, b"\r\n\r\n")).startswith(b"HTTP/1.1 400")
    await b.close()


async def test_connect_hands_off_with_the_bytes_that_followed() -> None:
    b = bench()
    tunnels: list[InterceptedConnection] = []
    client = b.proxy(b.context("http/1.1", target=None), proxy_mode(tunnels=tunnels))
    await client.write(b"CONNECT example.org:443 HTTP/1.1\r\nHost: example.org:443\r\n\r\nEARLY")
    assert (await read_until(client, b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
    tunnel = tunnels[0]
    assert (tunnel.target_host, tunnel.target_port) == ("example.org", 443)
    assert await tunnel.stream.read() == b"EARLY"
    await b.close()


async def test_connect_reject_and_bad_targets() -> None:
    b = bench()

    async def on_connect(conn: InterceptedConnection) -> None:
        await conn.reject(502, "cannot reach example.org")

    client = b.proxy(b.context("http/1.1", target=None), ProxyMode(auth="", on_connect=on_connect))
    await client.write(b"CONNECT example.org:443 HTTP/1.1\r\nHost: example.org:443\r\n\r\n")
    data = await read_until(client, b"example.org\n")
    assert data.startswith(b"HTTP/1.1 502") and b"cannot reach" in data

    client2 = b.proxy(b.context("http/1.1", target=None), proxy_mode())
    await client2.write(b"CONNECT example.org HTTP/1.1\r\nHost: example.org\r\n\r\n")
    assert (await read_until(client2, b"\r\n\r\n")).startswith(b"HTTP/1.1 400")

    client3 = b.proxy(b.context("http/1.1"))  # not a proxy listener
    await client3.write(b"CONNECT example.org:443 HTTP/1.1\r\nHost: example.org:443\r\n\r\n")
    assert (await read_until(client3, b"\r\n\r\n")).startswith(b"HTTP/1.1 405")
    await b.close()


class H2Client:
    def __init__(self, stream: MemoryStream) -> None:
        self.stream = stream
        self.conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))

    async def start(self) -> None:
        self.conn.initiate_connection()
        await self.flush()

    async def flush(self) -> None:
        await self.stream.write(self.conn.data_to_send())

    async def events(self, until: type) -> list[h2.events.Event]:
        seen: list[h2.events.Event] = []
        while not any(isinstance(e, until) for e in seen):
            data = await self.stream.read()
            if not data:
                break
            seen += self.conn.receive_data(data)
            await self.flush()
        return seen


async def test_h2_connect_is_501() -> None:
    b = bench()
    client = H2Client(b.proxy(b.context("h2")))
    await client.start()
    client.conn.send_headers(
        1, [(":method", "CONNECT"), (":authority", "example.org:443")], end_stream=True
    )
    await client.flush()
    events = await client.events(h2.events.StreamEnded)
    response = next(e for e in events if isinstance(e, h2.events.ResponseReceived))
    assert (b":status", b"501") in (response.headers or [])
    await b.close()


async def test_h2_client_reset_and_protocol_error() -> None:
    b = bench(Origin(body=b"x" * 10))
    stream = b.proxy(b.context("h2"))
    client = H2Client(stream)
    await client.start()
    client.conn.send_headers(
        1,
        [(":method", "POST"), (":scheme", "https"), (":authority", "example.org"), (":path", "/")],
    )
    await client.flush()
    client.conn.reset_stream(1)
    await client.flush()
    await asyncio.sleep(0.01)
    await stream.write(b"\x00\x00\x01\x09\x00\x00\x00\x00\x01Z")  # CONTINUATION out of place
    events = await client.events(h2.events.ConnectionTerminated)
    assert any(isinstance(e, h2.events.ConnectionTerminated) for e in events)
    await b.close()
