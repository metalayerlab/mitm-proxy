"""End-to-end through the proxy's exchange, in memory, for every protocol pairing."""

import asyncio

import pytest

from mitm_proxy.errors import UpstreamConnectError, UpstreamTimeout, UpstreamTLSError
from mitm_proxy.http.body import BufferedBodyStream
from mitm_proxy.http.flow import Flow, Response
from mitm_proxy.http.h1 import H1Upstream
from mitm_proxy.http.headers import Headers
from mitm_proxy.pipeline import Action, Middleware
from mitm_proxy.scope import Allowlist
from tests.harness import Origin, bench, client_for, send

PAIRS = [("http/1.1", "http/1.1"), ("http/1.1", "h2"), ("h2", "http/1.1"), ("h2", "h2")]


class Upper(Middleware):
    async def on_response_chunk(self, flow: Flow, chunk: bytes, is_last: bool) -> bytes | None:
        return chunk.upper()

    async def on_request_chunk(self, flow: Flow, chunk: bytes, is_last: bool) -> bytes | None:
        return chunk + b"+mutated"


class Watch(Middleware):
    """Applies, but never mutates: bodies are buffered, framing stays length-based."""

    def __init__(self) -> None:
        self.flows: list[Flow] = []

    async def on_request(self, flow: Flow) -> Action:
        self.flows.append(flow)
        return Action.CONTINUE


@pytest.mark.parametrize(("client_proto", "origin_proto"), PAIRS)
async def test_request_and_response_cross_protocols(client_proto: str, origin_proto: str) -> None:
    b = bench(Origin(protocol=origin_proto, echo=True))
    client = await client_for(b.proxy(b.context(client_proto)), client_proto)
    reply = await send(
        client, "POST", "/submit?x=1", body=b"payload", headers=[("x-test", "1"), ("cookie", "a=1")]
    )
    assert reply.status == 200
    assert reply.body == b"payload"
    seen = b.origin.seen[0]
    assert (seen.method, seen.authority, seen.path) == ("POST", "example.org", "/submit?x=1")
    assert seen.headers.get("x-test") == "1"
    assert "connection" not in seen.raw_headers and "proxy-connection" not in seen.raw_headers
    await b.close()


@pytest.mark.parametrize(("client_proto", "origin_proto"), PAIRS)
async def test_mutation_forces_length_independent_framing(
    client_proto: str, origin_proto: str
) -> None:
    b = bench(Origin(protocol=origin_proto, echo=True))
    client = await client_for(b.proxy(b.context(client_proto, [("upper", Upper())])), client_proto)
    reply = await send(client, "POST", "/", body=b"abc")
    assert reply.body == b"ABC+MUTATED"
    seen = b.origin.seen[0]
    assert seen.body == b"abc+mutated"
    if origin_proto == "http/1.1":
        assert seen.raw_headers.get("transfer-encoding") == "chunked"
    assert "content-length" not in seen.raw_headers
    if isinstance(client, H1Upstream):
        assert client.last_raw_headers.get("transfer-encoding") == "chunked"
        assert "content-length" not in client.last_raw_headers
    else:
        assert "content-length" not in reply.headers
    await b.close()


async def test_no_middleware_passes_content_length_through() -> None:
    b = bench(Origin())
    client = await client_for(b.proxy(b.context("http/1.1")), "http/1.1")
    reply = await send(client)
    assert isinstance(client, H1Upstream)
    assert client.last_raw_headers.get("content-length") == str(len(b"hello from origin"))
    assert reply.body == b"hello from origin"
    await b.close()


async def test_watching_middleware_keeps_length_framing() -> None:
    b = bench(Origin(chunked=True))
    watch = Watch()
    client = await client_for(b.proxy(b.context("http/1.1", [("watch", watch)])), "http/1.1")
    reply = await send(client)
    assert isinstance(client, H1Upstream)
    assert client.last_raw_headers.get("content-length") == str(len(reply.body))
    assert watch.flows[0].server_conn is not None
    await b.close()


@pytest.mark.parametrize("status", [204, 304])
async def test_bodiless_statuses_never_get_framing(status: int) -> None:
    b = bench(Origin(status=status, body=b""))
    client = await client_for(b.proxy(b.context("http/1.1", [("upper", Upper())])), "http/1.1")
    reply = await send(client)
    assert reply.status == status
    assert isinstance(client, H1Upstream)
    assert "transfer-encoding" not in client.last_raw_headers
    await b.close()


async def test_head_keeps_its_length_and_gets_no_chunking() -> None:
    b = bench(Origin())
    client = await client_for(b.proxy(b.context("http/1.1", [("upper", Upper())])), "http/1.1")
    reply = await send(client, "HEAD")
    assert reply.body == b""
    assert isinstance(client, H1Upstream)
    # The origin's length describes the GET body; HEAD keeps it, and gets no chunking.
    assert client.last_raw_headers.get("content-length") == str(len(b"hello from origin"))
    assert "transfer-encoding" not in client.last_raw_headers
    await b.close()


async def test_request_deny() -> None:
    class Deny(Middleware):
        async def on_request(self, flow: Flow) -> Action:
            return Action.DENY

    b = bench()
    client = await client_for(b.proxy(b.context("h2", [("guard", Deny())])), "h2")
    reply = await send(client)
    assert reply.status == 403
    assert reply.body == b"denied by middleware guard\n"
    assert b.origin.seen == []
    await b.close()


async def test_response_deny() -> None:
    class Deny(Middleware):
        async def on_response(self, flow: Flow) -> Action:
            return Action.DENY

    b = bench()
    client = await client_for(b.proxy(b.context("http/1.1", [("guard", Deny())])), "http/1.1")
    reply = await send(client)
    assert (reply.status, reply.body) == (502, b"denied by middleware guard\n")
    await b.close()


async def test_stop_with_a_response_skips_the_origin() -> None:
    class Cache(Middleware):
        async def on_request(self, flow: Flow) -> Action:
            flow.response = Response.make(200, b"cached")
            return Action.STOP

    b = bench()
    client = await client_for(b.proxy(b.context("http/1.1", [("cache", Cache())])), "http/1.1")
    reply = await send(client)
    assert (reply.status, reply.body) == (200, b"cached")
    assert b.origin.connections == 0
    await b.close()


async def test_stop_without_a_response_is_a_502() -> None:
    class Broken(Middleware):
        async def on_request(self, flow: Flow) -> Action:
            return Action.STOP

    b = bench()
    client = await client_for(b.proxy(b.context("http/1.1", [("broken", Broken())])), "http/1.1")
    assert (await send(client)).status == 502
    await b.close()


async def test_a_middleware_can_replace_a_body_by_assignment() -> None:
    class Replace(Middleware):
        async def on_request(self, flow: Flow) -> Action:
            flow.request.body = BufferedBodyStream(b"new body")
            return Action.CONTINUE

        async def on_response(self, flow: Flow) -> Action:
            assert flow.response is not None
            flow.response.body = BufferedBodyStream(b"short")
            return Action.CONTINUE

    b = bench(Origin(echo=True))
    client = await client_for(b.proxy(b.context("http/1.1", [("r", Replace())])), "http/1.1")
    reply = await send(client, "POST", body=b"original")
    assert b.origin.seen[0].body == b"new body"
    assert reply.body == b"short"
    assert isinstance(client, H1Upstream)
    assert client.last_raw_headers.get("content-length") == "5"
    await b.close()


async def test_oversize_bodies_stream_through_without_middleware() -> None:
    b = bench(Origin(echo=True))
    upper = Upper()
    client = await client_for(
        b.proxy(b.context("http/1.1", [("upper", upper)], max_buffer=4)), "http/1.1"
    )
    reply = await send(client, "POST", body=b"0123456789")
    assert b.origin.seen[0].body == b"0123456789"
    assert reply.body == b"0123456789"
    assert b.metrics.oversize_passthrough.values == {("request",): 1.0}
    await b.close()


async def test_oversize_response_only() -> None:
    b = bench(Origin(body=b"a long response body"))
    client = await client_for(b.proxy(b.context("h2", [("upper", Upper())], max_buffer=8)), "h2")
    reply = await send(client)
    assert reply.body == b"a long response body"
    assert b.metrics.oversize_passthrough.values == {("response",): 1.0}
    await b.close()


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (UpstreamTLSError("example.org: certificate verification failed: self-signed"), 502),
        (UpstreamTimeout("connect timed out"), 504),
        (UpstreamConnectError("refused"), 502),
    ],
)
async def test_upstream_errors_become_proxy_responses(error: Exception, status: int) -> None:
    origin = Origin()
    origin.fail_connect = error  # type: ignore[assignment]  # UpstreamError subclasses
    b = bench(origin)
    client = await client_for(b.proxy(b.context("http/1.1")), "http/1.1")
    reply = await send(client)
    assert reply.status == status
    assert str(error) in reply.body.decode()
    await b.close()


async def test_h1_upstream_connection_is_reused() -> None:
    b = bench(Origin())
    client = await client_for(b.proxy(b.context("http/1.1")), "http/1.1")
    for _ in range(3):
        assert (await send(client)).status == 200
    assert b.origin.connections == 1
    assert b.pool.counts() == {"http/1.1": 1, "h2": 0}
    await b.close()


async def test_h2_upstream_is_shared_by_concurrent_streams() -> None:
    b = bench(Origin(protocol="h2"))
    client = await client_for(b.proxy(b.context("h2")), "h2")
    replies = await asyncio.gather(*(send(client, path=f"/{i}") for i in range(5)))
    assert [r.status for r in replies] == [200] * 5
    assert b.origin.connections == 1
    assert sorted(s.path for s in b.origin.seen) == [f"/{i}" for i in range(5)]
    await b.close()


async def test_trailers_cross_h2_to_h2() -> None:
    b = bench(Origin(protocol="h2", trailers=Headers([("grpc-status", "0")])))
    client = await client_for(b.proxy(b.context("h2")), "h2")
    reply = await send(
        client, "POST", body=b"x", headers=[("te", "trailers")], trailers=Headers([("x-req", "1")])
    )
    assert reply.trailers == Headers([("grpc-status", "0")])
    assert b.origin.seen[0].trailers == Headers([("x-req", "1")])
    await b.close()


async def test_trailers_dropped_for_an_h1_client_that_did_not_ask() -> None:
    b = bench(Origin(protocol="h2", trailers=Headers([("grpc-status", "0")])))
    client = await client_for(b.proxy(b.context("http/1.1")), "http/1.1")
    reply = await send(client)
    assert reply.trailers is None
    await b.close()


async def test_plain_http_proxying_uses_scope_per_request() -> None:
    b = bench(Origin(echo=True))
    upper = Upper()
    ctx = b.context("http/1.1", [("upper", upper)], target=None, scope=Allowlist(["example.org"]))
    client = await client_for(b.proxy(ctx), "http/1.1")
    inside = await send(client, "POST", "http://example.org/a", body=b"in")
    outside = await send(
        client, "POST", "http://example.net/b", body=b"out", authority="example.net"
    )
    assert inside.body == b"IN+MUTATED"
    assert outside.body == b"out"
    assert b.metrics.flows.values == {("plain_http",): 2.0}
    await b.close()


async def test_websocket_upgrade_is_relayed_raw() -> None:
    b = bench(Origin(websocket=True))
    watch = Watch()
    client_stream = b.proxy(b.context("http/1.1", [("watch", watch)]))
    await client_stream.write(
        b"GET /ws HTTP/1.1\r\nHost: example.org\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n"
        b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    head = b""
    while b"\r\n\r\n" not in head:
        head += await client_stream.read()
    assert head.startswith(b"HTTP/1.1 101")
    await client_stream.write(b"frames")
    assert await client_stream.read() == b"FRAMES"
    assert watch.flows == []  # no hooks for WebSocket flows
    assert b.metrics.flows.values == {("websocket",): 1.0}
    assert b.origin.seen[0].raw_headers.get("sec-websocket-key") == "dGhlIHNhbXBsZSBub25jZQ=="
    await client_stream.close()
    await b.close()


async def test_websocket_refused_by_the_origin_is_answered_normally() -> None:
    b = bench(Origin(websocket=False, status=403, body=b"no"))
    client_stream = b.proxy(b.context("http/1.1"))
    await client_stream.write(
        b"GET /ws HTTP/1.1\r\nHost: example.org\r\n"
        b"Connection: Upgrade\r\nUpgrade: websocket\r\n\r\n"
    )
    data = b""
    while not data.endswith(b"no"):
        data += await client_stream.read()
    assert data.startswith(b"HTTP/1.1 403")
    await b.close()


async def test_retry_once_when_a_reused_h1_connection_died() -> None:
    b = bench(Origin())
    client = await client_for(b.proxy(b.context("http/1.1")), "http/1.1")
    assert (await send(client)).status == 200
    # Kill the pooled connection's origin side behind the pool's back.
    for conns in b.pool._connections.values():
        for conn in conns:
            assert isinstance(conn, H1Upstream)
            peer = conn.stream.peer  # type: ignore[attr-defined]  # MemoryStream in tests
            await peer.close()
    assert (await send(client)).status == 200
    assert b.origin.connections == 2
    await b.close()
