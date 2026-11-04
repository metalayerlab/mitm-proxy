"""Routing in step 1 of design 0013: every upstream connection through the first default exit,
refusals that fail closed, and nothing changed while `exits` is empty."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from mitm_proxy.errors import UpstreamConnectError
from mitm_proxy.exchange import ExchangeContext
from mitm_proxy.http.flow import Flow
from mitm_proxy.http.h1 import H1Upstream
from mitm_proxy.pipeline import Action, Middleware
from mitm_proxy.routing import DIRECT_ROUTE, Exit, Router
from mitm_proxy.server import ProxyServer
from mitm_proxy.streams import ByteStream
from mitm_proxy.tls.terminate import TLSStream
from mitm_proxy.upstream.pool import Connected, Target
from tests.exits import FakeHTTPExit, FakeSocksExit, TestOrigin
from tests.harness import Origin, bench, send
from tests.helpers import MemoryStream, memory_pair, trusting_client_context
from tests.unit.test_reverse import free_port
from tests.unit.test_server import make_config

EXIT_A = {"MITM_PROXY_EXITS_0_NAME": "exit-a", "MITM_PROXY_EXITS_0_URL": "http://exit-a.exits:8001"}
EXIT_B = {"MITM_PROXY_EXITS_1_NAME": "exit-b", "MITM_PROXY_EXITS_1_URL": "http://exit-b.exits:8002"}


class Recording:
    """A connector that records each target, then connects to the fake origin (or fails)."""

    def __init__(self, origin: Origin | None = None) -> None:
        self.origin = origin or Origin()
        self.targets: list[Target] = []

    async def __call__(self, target: Target, alpn: tuple[str, ...], timeout: float) -> Connected:
        self.targets.append(target)
        return await self.origin.connector(target, alpn, timeout)


async def read_head(stream: ByteStream) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await stream.read()
        if not chunk:
            break
        data += chunk
    return data


async def through_source(
    proxy: ProxyServer, request: bytes
) -> tuple[MemoryStream, bytes, asyncio.Task[None]]:
    client, proxy_end = memory_pair()
    task = asyncio.create_task(proxy.source.handle_client(proxy_end, ("192.0.2.1", 5000)))
    await client.write(request)
    return client, await read_head(client), task


# The router


def test_the_router_is_off_without_exits() -> None:
    router = Router()
    assert not router.enabled
    assert router.route("example.org") is DIRECT_ROUTE
    assert DIRECT_ROUTE.label == "direct"


def test_the_router_uses_the_first_default_exit_and_never_another() -> None:
    a, b = Exit("exit-a", "http", "exit-a.exits", 8001), Exit("exit-b", "http", "exit-b.exits", 1)
    route = Router([a, b], ["exit-a", "exit-b"]).route("example.org")
    assert (route.exit, route.refused, route.label) == (a, None, "exit-a")


def test_the_router_refuses_everything_without_a_default() -> None:
    route = Router([Exit("exit-a", "http", "exit-a.exits", 8001)]).route("example.org")
    assert (route.exit, route.refused, route.label) == (None, "no_rule", "")
    assert "routing.default_via is empty" in route.detail


# Exits empty: as before 0013


async def test_without_exits_every_connection_is_direct_and_metrics_are_unchanged(
    tmp_path: Path,
) -> None:
    connector = Recording()
    proxy = ProxyServer(make_config(tmp_path), connector=connector)
    assert not proxy.snapshot.router.enabled
    client, head, task = await through_source(
        proxy, b"GET http://example.net/x HTTP/1.1\r\nHost: example.net\r\n\r\n"
    )
    assert head.startswith(b"HTTP/1.1 200")
    assert [(t.host, t.via, t.tunnel) for t in connector.targets] == [("example.net", None, False)]
    assert "route_refused" not in proxy.metrics.render()
    await client.close()
    await asyncio.wait_for(task, 2)
    await proxy.stop()


# Exits set: every path through the first default exit


async def test_a_relayed_tunnel_goes_through_the_exit(tmp_path: Path) -> None:
    connector = Recording()
    config = make_config(
        tmp_path, **EXIT_A, **EXIT_B, MITM_PROXY_ROUTING_DEFAULT_VIA="exit-b,exit-a"
    )
    proxy = ProxyServer(config, connector=connector)
    client, head, task = await through_source(
        proxy, b"CONNECT example.net:443 HTTP/1.1\r\nHost: example.net:443\r\n\r\n"
    )
    assert head.startswith(b"HTTP/1.1 200")
    (target,) = connector.targets
    assert (target.host, target.port, target.tunnel) == ("example.net", 443, True)
    assert target.via is not None and target.via.name == "exit-b"
    await client.close()
    await asyncio.wait_for(task, 2)
    await proxy.stop()


async def test_an_intercepted_flow_goes_through_the_exit(tmp_path: Path) -> None:
    connector = Recording()
    config = make_config(tmp_path, **EXIT_A, MITM_PROXY_ROUTING_DEFAULT_VIA="exit-a")
    proxy = ProxyServer(config, connector=connector)
    client, head, task = await through_source(
        proxy, b"CONNECT example.org:443 HTTP/1.1\r\nHost: example.org:443\r\n\r\n"
    )
    assert head.startswith(b"HTTP/1.1 200")
    tls = TLSStream(
        client,
        trusting_client_context(proxy.ca, ["http/1.1"]),
        server_side=False,
        server_hostname="example.org",
    )
    await tls.handshake()
    h1 = H1Upstream(tls, read_timeout=5)
    assert (await send(h1, path="/in")).status == 200
    (target,) = connector.targets
    assert (target.host, target.tls, target.tunnel) == ("example.org", True, False)
    assert target.via is not None and target.via.name == "exit-a"
    await h1.close()
    await asyncio.wait_for(task, 2)
    await proxy.stop()


async def test_plain_http_goes_through_the_exit(tmp_path: Path) -> None:
    connector = Recording()
    config = make_config(tmp_path, **EXIT_A, MITM_PROXY_ROUTING_DEFAULT_VIA="exit-a")
    proxy = ProxyServer(config, connector=connector)
    client, head, task = await through_source(
        proxy, b"GET http://example.net/x HTTP/1.1\r\nHost: example.net\r\n\r\n"
    )
    assert head.startswith(b"HTTP/1.1 200")
    (target,) = connector.targets
    assert target.via is not None and target.via.name == "exit-a"
    assert target.absolute_form
    await client.close()
    await asyncio.wait_for(task, 2)
    await proxy.stop()


async def test_a_reverse_listener_goes_through_the_exit(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        **EXIT_A,
        MITM_PROXY_ROUTING_DEFAULT_VIA="exit-a",
        MITM_PROXY_REVERSE_0_LISTEN=f"127.0.0.1:{free_port()}",
        MITM_PROXY_REVERSE_0_UPSTREAM="http://origin.test:7000",
    )
    proxy = ProxyServer(config, connector=Recording())
    ((_listener, source),) = proxy.reverse_sources
    ctx: ExchangeContext = source.context(("192.0.2.1", 5000))
    assert ctx.target is not None and ctx.target.via is not None
    assert (ctx.target.host, ctx.target.via.name) == ("origin.test", "exit-a")
    await proxy.stop()


async def test_direct_is_used_only_when_allowed_and_chosen(tmp_path: Path) -> None:
    connector = Recording()
    config = make_config(
        tmp_path,
        **EXIT_A,
        MITM_PROXY_ROUTING_DEFAULT_VIA="direct,exit-a",
        MITM_PROXY_ROUTING_ALLOW_DIRECT="true",
    )
    proxy = ProxyServer(config, connector=connector)
    client, head, task = await through_source(
        proxy, b"CONNECT example.net:443 HTTP/1.1\r\nHost: example.net:443\r\n\r\n"
    )
    assert head.startswith(b"HTTP/1.1 200")
    assert connector.targets[0].via is None
    await client.close()
    await asyncio.wait_for(task, 2)
    await proxy.stop()


async def test_an_exit_that_fails_fails_the_request_and_nothing_goes_direct(
    tmp_path: Path,
) -> None:
    origin = Origin()
    origin.fail_connect = UpstreamConnectError("exit exit-a: the connection failed")
    connector = Recording(origin)
    config = make_config(
        tmp_path, **EXIT_A, **EXIT_B, MITM_PROXY_ROUTING_DEFAULT_VIA="exit-a,exit-b"
    )
    proxy = ProxyServer(config, connector=connector)
    for request in (
        b"CONNECT example.net:443 HTTP/1.1\r\nHost: example.net:443\r\n\r\n",
        b"GET http://example.net/x HTTP/1.1\r\nHost: example.net\r\n\r\n",
    ):
        client, head, task = await through_source(proxy, request)
        assert head.startswith(b"HTTP/1.1 502")
        await client.close()
        await asyncio.wait_for(task, 2)
    # Once each, through exit-a: no failover (step 5), and never direct.
    assert [t.via.name if t.via else None for t in connector.targets] == ["exit-a", "exit-a"]
    await proxy.stop()


# Exits set, no default: fail closed


def refusing(tmp_path: Path) -> tuple[ProxyServer, Recording]:
    connector = Recording()
    return ProxyServer(make_config(tmp_path, **EXIT_A), connector=connector), connector


async def test_a_connect_with_no_route_is_a_503_and_never_forwarded(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    proxy, connector = refusing(tmp_path)
    client, head, task = await through_source(
        proxy, b"CONNECT example.net:443 HTTP/1.1\r\nHost: example.net:443\r\n\r\n"
    )
    assert head.startswith(b"HTTP/1.1 503")
    lower = head.lower()
    assert b"\r\nretry-after: 30\r\n" in lower
    assert b"\r\nproxy-status: mitm-proxy; error=destination_unavailable; details=" in lower
    assert connector.targets == []
    assert proxy.metrics.render().count(
        'mitm_proxy_route_refused_total{exit="",reason="no_rule"} 1'
    )
    assert "routing refused example.net" in caplog.text
    await asyncio.wait_for(task, 2)
    await client.close()
    await proxy.stop()


async def test_plain_http_with_no_route_is_a_local_503(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    proxy, connector = refusing(tmp_path)
    client, head, task = await through_source(
        proxy, b"GET http://example.net/secret/path HTTP/1.1\r\nHost: example.net\r\n\r\n"
    )
    assert head.startswith(b"HTTP/1.1 503")
    assert b"\r\nretry-after: 30\r\n" in head.lower()
    assert connector.targets == []
    assert "routing refused example.net" in caplog.text
    assert "/secret/path" not in caplog.text  # the site, never the path
    await client.close()
    await asyncio.wait_for(task, 2)
    await proxy.stop()


class Outcomes(Middleware):
    """Records what on_done sees, and whether any request hook ran."""

    def __init__(self) -> None:
        self.outcomes: list[str] = []
        self.requests = 0

    async def on_request(self, flow: Flow) -> Action:
        self.requests += 1
        return Action.CONTINUE

    async def on_done(self, flow: Flow) -> None:
        self.outcomes.append(str(flow.outcome))


async def test_an_inspected_flow_with_no_route_is_a_local_503_by_routing(tmp_path: Path) -> None:
    connector = Recording()
    seen = Outcomes()
    proxy = ProxyServer(
        make_config(tmp_path, **EXIT_A), connector=connector, middleware=[("seen", seen)]
    )
    client, head, task = await through_source(
        proxy, b"CONNECT example.org:443 HTTP/1.1\r\nHost: example.org:443\r\n\r\n"
    )
    assert head.startswith(b"HTTP/1.1 200")  # intercepted: answered inside, per request
    tls = TLSStream(
        client,
        trusting_client_context(proxy.ca, ["http/1.1"]),
        server_side=False,
        server_hostname="example.org",
    )
    await tls.handshake()
    h1 = H1Upstream(tls, read_timeout=5)
    reply = await send(h1, path="/in")
    assert reply.status == 503
    assert reply.headers.get("retry-after") == "30"
    assert b"not sent to example.org" in reply.body
    assert connector.targets == []
    for _ in range(100):  # on_done runs after the response has gone
        if seen.outcomes:
            break
        await asyncio.sleep(0.01)
    assert seen.outcomes == ["local:routing"]
    assert seen.requests == 0  # refused before any hook could act on it
    await h1.close()
    await asyncio.wait_for(task, 2)
    await proxy.stop()


async def test_an_accepted_tunnel_with_no_route_is_closed_never_relayed(tmp_path: Path) -> None:
    proxy, connector = refusing(tmp_path)
    client, head, task = await through_source(
        proxy, b"CONNECT example.org:80 HTTP/1.1\r\nHost: example.org:80\r\n\r\n"
    )
    # example.org is in the allowlist: accepted to peek, then not TLS, so it would be relayed.
    assert head.startswith(b"HTTP/1.1 200")
    await client.write(b"GET / HTTP/1.1\r\nHost: example.org\r\n\r\n")
    await asyncio.wait_for(task, 2)
    assert connector.targets == []
    assert await client.read() == b""
    await proxy.stop()


async def test_the_exchange_refuses_a_head_request_without_a_body() -> None:
    b = bench()
    ctx = b.context("http/1.1", target=None)
    ctx.router = Router([Exit("exit-a", "http", "exit-a.exits", 8001)])
    b.metrics.enable_routing(["exit-a"])
    client = H1Upstream(b.proxy(ctx), read_timeout=5)
    reply = await send(client, "HEAD", authority="example.net")
    assert (reply.status, reply.body) == (503, b"")
    assert b.origin.seen == []
    await client.close()
    await b.close()


# End to end, over loopback, with the real connectors and fake exits


async def proxy_request(port: int, request: bytes) -> tuple[bytes, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    return head + await asyncio.wait_for(reader.read(4096), 2), writer


@pytest.mark.parametrize("kind", ["http", "socks5h"])
async def test_end_to_end_through_a_fake_exit(tmp_path: Path, kind: str) -> None:
    origin = TestOrigin()
    fake: FakeHTTPExit | FakeSocksExit = (
        FakeHTTPExit(routes={"routed.example:80": await origin.start()})
        if kind == "http"
        else FakeSocksExit(routes={"routed.example:80": await origin.start()})
    )
    exit_port = await fake.start()
    config = make_config(
        tmp_path,
        MITM_PROXY_PROXY_PORT=str(free_port()),
        MITM_PROXY_EXITS_0_NAME="exit-a",
        MITM_PROXY_EXITS_0_URL=f"{kind}://127.0.0.1:{exit_port}",
        MITM_PROXY_ROUTING_DEFAULT_VIA="exit-a",
    )
    proxy = ProxyServer(config)
    await proxy.start()
    # A relayed tunnel: CONNECT through the exit, then raw bytes.
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
    writer.write(b"CONNECT routed.example:80 HTTP/1.1\r\nHost: routed.example:80\r\n\r\n")
    await writer.drain()
    assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
    writer.write(b"GET /tunnelled HTTP/1.1\r\nHost: routed.example\r\n\r\n")
    await writer.drain()
    answer = await reader.readuntil(b"hello from the origin")
    assert answer.startswith(b"HTTP/1.1 200")
    writer.close()
    # Plain HTTP: absolute form to an HTTP exit, origin form inside a SOCKS5 tunnel.
    reply, writer = await proxy_request(
        proxy.port, b"GET http://routed.example/plain HTTP/1.1\r\nHost: routed.example\r\n\r\n"
    )
    assert reply.startswith(b"HTTP/1.1 200")
    if kind == "http":
        assert reply.endswith(b"exit saw GET http://routed.example/plain")
    else:
        assert reply.endswith(b"hello from the origin")
        assert origin.requests == ["GET /tunnelled HTTP/1.1", "GET /plain HTTP/1.1"]
    writer.close()
    await proxy.stop()
    await fake.close()
    await origin.close()


async def test_end_to_end_refusal_over_a_socket(tmp_path: Path) -> None:
    config = make_config(tmp_path, MITM_PROXY_PROXY_PORT=str(free_port()), **EXIT_A)
    proxy = ProxyServer(config)
    await proxy.start()
    reply, writer = await proxy_request(
        proxy.port, b"CONNECT example.net:443 HTTP/1.1\r\nHost: example.net:443\r\n\r\n"
    )
    assert reply.startswith(b"HTTP/1.1 503")
    assert b"retry-after: 30" in reply.lower()
    writer.close()
    await proxy.stop()
