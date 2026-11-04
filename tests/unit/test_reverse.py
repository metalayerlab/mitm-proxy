"""The reverse-proxy source and its listeners (design 0012), in memory and over loopback."""

from __future__ import annotations

import asyncio
import logging
import socket
from pathlib import Path

import pytest

from mitm_proxy.errors import StartupError
from mitm_proxy.exchange import ExchangeContext
from mitm_proxy.http.flow import ClientConn, Flow
from mitm_proxy.http.h1 import H1Upstream
from mitm_proxy.interception.reverse import ReverseProxySource
from mitm_proxy.pipeline import Action, Middleware, Pipeline
from mitm_proxy.server import ProxyServer
from mitm_proxy.upstream.pool import Target
from tests.harness import Bench, Origin, Reply, bench, send
from tests.helpers import memory_pair
from tests.unit.test_server import make_config


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Recorder(Middleware):
    """Records each flow's direction when the request hooks run."""

    def __init__(self) -> None:
        self.directions: list[str] = []

    async def on_request(self, flow: Flow) -> Action:
        self.directions.append(flow.direction)
        return Action.CONTINUE


def reverse_source(
    b: Bench,
    *,
    upstream: Target,
    upstream_authority: str,
    upstream_scheme: str = "http",
    middleware: list[tuple[str, Middleware]] | None = None,
) -> ReverseProxySource:
    def context(client_addr: tuple[str, int]) -> ExchangeContext:
        b.metrics.declare_middleware(name for name, _ in middleware or [])
        return ExchangeContext(
            pool=b.pool,
            metrics=b.metrics,
            client_conn=ClientConn(client_addr),
            protocol="http/1.1",
            max_buffer=1 << 20,
            idle_timeout=5,
            pipeline=Pipeline(middleware or [], hook_timeout=1),
            target=upstream,
            scope=None,
            direction="inbound",
        )

    return ReverseProxySource(
        context=context,
        upstream_authority=upstream_authority,
        upstream_scheme=upstream_scheme,
        idle_timeout=5,
    )


async def through_reverse(
    source: ReverseProxySource,
    *,
    path: str = "/",
    authority: str = "client.example",
    headers: list[tuple[str, str]] | None = None,
    client_ip: str = "198.51.100.7",
) -> Reply:
    client_end, proxy_end = memory_pair()
    task = asyncio.create_task(source.handle_client(proxy_end, (client_ip, 4000)))
    client = H1Upstream(client_end, read_timeout=5)
    reply = await send(client, path=path, authority=authority, headers=headers)
    await client.close()
    await asyncio.wait_for(task, 2)
    return reply


async def test_reverse_forwards_to_the_upstream_with_forwarding_headers_and_inbound_direction() -> (
    None
):
    recorder = Recorder()
    b = bench(Origin(body=b"from upstream"))
    source = reverse_source(
        b,
        upstream=Target("api.internal", 8000, tls=False),
        upstream_authority="api.internal:8000",
        middleware=[("rec", recorder)],
    )
    reply = await through_reverse(source, path="/v1/chat", authority="front.example:443")
    assert reply.status == 200 and reply.body == b"from upstream"
    seen = b.origin.seen[0]
    # The origin is told the upstream's authority, not the client's.
    assert seen.authority == "api.internal:8000"
    assert seen.path == "/v1/chat"
    # The client's Host is preserved, and its address is in X-Forwarded-For.
    assert seen.headers.get("x-forwarded-host") == "front.example:443"
    assert seen.headers.get("x-forwarded-for") == "198.51.100.7"
    # No Forwarded and no X-Forwarded-Proto are added (design 0012, open question 5).
    assert "forwarded" not in seen.headers and "x-forwarded-proto" not in seen.headers
    assert recorder.directions == ["inbound"]
    await b.close()


async def test_x_forwarded_host_is_kept_when_the_client_already_set_one() -> None:
    b = bench()
    source = reverse_source(
        b, upstream=Target("api.internal", 8000, tls=False), upstream_authority="api.internal:8000"
    )
    await through_reverse(
        source,
        authority="front.example",
        headers=[("x-forwarded-host", "edge.example"), ("x-forwarded-for", "203.0.113.9")],
    )
    seen = b.origin.seen[0]
    assert seen.headers.get("x-forwarded-host") == "edge.example"  # not overwritten
    assert seen.headers.get("x-forwarded-for") == "203.0.113.9, 198.51.100.7"  # appended
    await b.close()


async def test_a_forward_proxy_flow_has_egress_direction(tmp_path: Path) -> None:
    recorder = Recorder()
    proxy = ProxyServer(
        make_config(tmp_path), middleware=[("rec", recorder)], connector=Origin().connector
    )
    client_raw, proxy_raw = memory_pair()
    task = asyncio.create_task(proxy.source.handle_client(proxy_raw, ("192.0.2.1", 5000)))
    client = H1Upstream(client_raw, read_timeout=5)
    # Absolute-form to a listed host: the forward proxy intercepts and runs the pipeline.
    reply = await send(client, path="http://example.org/x", authority="example.org")
    assert reply.status == 200
    assert recorder.directions == ["egress"]
    await client.close()
    await asyncio.wait_for(task, 2)
    await proxy.stop()


async def test_reverse_listener_is_bound_at_startup_and_ready(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        MITM_PROXY_PROXY_PORT=str(free_port()),
        MITM_PROXY_REVERSE_0_LISTEN=f"127.0.0.1:{free_port()}",
        MITM_PROXY_REVERSE_0_UPSTREAM="http://origin:7000",
    )
    proxy = ProxyServer(config, connector=Origin().connector)
    assert proxy.state == "starting"
    await proxy.start()
    try:
        assert proxy.state == "ready"
        assert len(proxy._reverse_servers) == 1 and proxy._reverse_servers[0].is_serving()
    finally:
        await proxy.stop()
    assert not proxy._reverse_servers[0].is_serving()


async def test_a_reverse_bind_failure_is_a_startup_error(tmp_path: Path) -> None:
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen()
    port = held.getsockname()[1]
    try:
        config = make_config(
            tmp_path,
            MITM_PROXY_PROXY_PORT=str(free_port()),
            MITM_PROXY_REVERSE_0_LISTEN=f"127.0.0.1:{port}",
            MITM_PROXY_REVERSE_0_UPSTREAM="http://origin:7000",
        )
        proxy = ProxyServer(config, connector=Origin().connector)
        with pytest.raises(StartupError, match=f"cannot listen on 127.0.0.1:{port}"):
            await proxy.start()
        assert proxy.state != "ready"  # readiness waits for every listener to bind
        await proxy.stop()
    finally:
        held.close()


async def test_a_reverse_change_on_reload_warns_and_keeps_the_running_listeners(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    config = make_config(
        tmp_path,
        MITM_PROXY_REVERSE_0_LISTEN="127.0.0.1:8081",
        MITM_PROXY_REVERSE_0_UPSTREAM="http://origin:7000",
    )
    proxy = ProxyServer(config, connector=Origin().connector)
    before = proxy.reverse_sources
    changed = make_config(
        tmp_path,
        MITM_PROXY_REVERSE_0_LISTEN="127.0.0.1:8081",
        MITM_PROXY_REVERSE_0_UPSTREAM="http://origin:9000",
    )
    proxy.apply(changed)
    assert "reverse changed" in caplog.text and "restart" in caplog.text
    assert proxy.reverse_sources is before  # the running listeners are untouched
    await proxy.stop()
