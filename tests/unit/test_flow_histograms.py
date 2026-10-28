"""The flow duration histograms (design 0007), as the exchange fills them."""

import asyncio

from mitm_proxy.http.flow import Flow, Response
from mitm_proxy.metrics import Histogram, Metrics
from mitm_proxy.pipeline import Action, Middleware
from mitm_proxy.scope import Allowlist
from tests.harness import Origin, bench, client_for, send


def _count(h: Histogram, *labels: str) -> int:
    series = h.series.get(labels)
    return sum(series[0]) if series else 0


def _sum(h: Histogram, *labels: str) -> float:
    series = h.series.get(labels)
    return series[1][0] if series else 0.0


async def _observed(m: Metrics, count: int) -> None:
    """A flow is observed just after the client has its last byte."""
    while sum(sum(c) for c, _ in m.flow_duration.series.values()) < count:
        await asyncio.sleep(0)


async def test_origin_flows_fill_duration_connect_and_ttfb() -> None:
    b = bench(Origin(delay=0.03))
    client = await client_for(b.proxy(b.context("h2")), "h2")
    await send(client)
    await send(client)
    await _observed(b.metrics, 2)
    m = b.metrics
    assert _count(m.flow_duration, "origin") == 2
    assert (_count(m.upstream_connect, "false"), _count(m.upstream_connect, "true")) == (1, 1)
    assert _count(m.origin_ttfb) == 2 and _sum(m.origin_ttfb) >= 0.06
    assert m.middleware_duration.series == {}  # no middleware ran
    await b.close()


async def test_local_answers_are_kept_apart_by_outcome() -> None:
    class Cache(Middleware):
        async def on_request(self, flow: Flow) -> Action:
            if flow.request.path == "/hit":
                flow.response = Response.make(200, b"cached")
                return Action.STOP
            return Action.CONTINUE

    b = bench()
    client = await client_for(b.proxy(b.context("http/1.1", [("cache", Cache())])), "http/1.1")
    await send(client, path="/hit")
    await send(client, path="/miss")
    await _observed(b.metrics, 2)
    m = b.metrics
    assert (_count(m.flow_duration, "local"), _count(m.flow_duration, "origin")) == (1, 1)
    assert _count(m.origin_ttfb) == 1  # only the miss reached the origin
    assert _count(m.middleware_duration, "cache") == 2
    await b.close()


async def test_middleware_time_includes_on_done() -> None:
    class Audit(Middleware):
        async def on_done(self, flow: Flow) -> None:
            await asyncio.sleep(0.03)

    b = bench()
    client = await client_for(b.proxy(b.context("h2", [("audit", Audit())])), "h2")
    await send(client)
    await _observed(b.metrics, 1)
    assert _count(b.metrics.middleware_duration, "audit") == 1
    assert _sum(b.metrics.middleware_duration, "audit") >= 0.03
    await b.close()


async def test_websockets_and_relayed_flows_are_not_observed() -> None:
    b = bench(Origin(websocket=True, echo=True))
    stream = b.proxy(b.context("http/1.1"))
    await stream.write(
        b"GET /ws HTTP/1.1\r\nHost: example.org\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n"
        b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    head = b""
    while b"\r\n\r\n" not in head:
        head += await stream.read()
    await stream.close()
    plain = b.context("http/1.1", target=None, scope=Allowlist(["example.org"]))
    client = await client_for(b.proxy(plain), "http/1.1")
    await send(client, path="http://example.net/", authority="example.net")
    await send(client, path="http://example.org/")  # intercepted: the one observation
    await _observed(b.metrics, 1)
    await asyncio.sleep(0.01)
    assert b.metrics.flow_duration.series.keys() == {("origin",)}
    assert _count(b.metrics.flow_duration, "origin") == 1
    await b.close()
