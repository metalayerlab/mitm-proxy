"""upstream.read_timeout_ms: the wait for an origin's bytes, apart from the client idle timeout.

Design 0012 section 5. The slow-origin tests run on a virtual clock (tests.helpers.run_virtual):
an origin silent for five minutes takes moments, and the timeouts are the real configured ones.
"""

import asyncio
import logging
from pathlib import Path

import pytest

from mitm_proxy.config import SETTINGS, ConfigError, load_config
from mitm_proxy.errors import UpstreamConnectError, UpstreamError, UpstreamTimeout
from mitm_proxy.exchange import ExchangeContext
from mitm_proxy.http.h1 import H1Upstream
from mitm_proxy.http.h2 import H2Upstream
from mitm_proxy.server import ProxyServer
from mitm_proxy.tls.terminate import TLSStream
from mitm_proxy.upstream.pool import Target, TCPConnector
from tests.harness import Bench, Origin, Reply, bench, send
from tests.helpers import memory_pair, run_virtual, trusting_client_context
from tests.unit.test_server import make_config, tunnel

# The settings' values, in milliseconds.
FIVE_MINUTES = "300000"
TWO_MINUTES = "120000"  # proxy.idle_timeout_ms's default


def test_the_default_is_the_idle_timeouts_default(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "missing.yaml", environ={})
    assert cfg.upstream_read_timeout_ms == 120000 == cfg.idle_timeout_ms
    assert cfg.sources["upstream.read_timeout_ms"] == "default"
    setting = next(s for s in SETTINGS if s.key == "upstream.read_timeout_ms")
    assert not setting.secret


def test_it_is_set_apart_from_the_idle_timeout(tmp_path: Path) -> None:
    env = {"MITM_PROXY_UPSTREAM_READ_TIMEOUT_MS": FIVE_MINUTES}
    cfg = load_config(tmp_path / "missing.yaml", environ=env)
    assert (cfg.upstream_read_timeout_ms, cfg.idle_timeout_ms) == (300000, 120000)


@pytest.mark.parametrize("value", ["0", "-1"])
def test_it_must_be_positive(tmp_path: Path, value: str) -> None:
    env = {"MITM_PROXY_UPSTREAM_READ_TIMEOUT_MS": value}
    with pytest.raises(ConfigError, match="read_timeout_ms: must be greater than 0"):
        load_config(tmp_path / "x.yaml", environ=env)


def test_it_must_be_an_int(tmp_path: Path) -> None:
    env = {"MITM_PROXY_UPSTREAM_READ_TIMEOUT_MS": "slow"}
    with pytest.raises(ConfigError, match="read_timeout_ms: expected int"):
        load_config(tmp_path / "x.yaml", environ=env)


async def test_the_pool_reads_with_it_at_startup_and_after_a_reload(tmp_path: Path) -> None:
    origin = Origin()
    proxy = ProxyServer(
        make_config(
            tmp_path,
            MITM_PROXY_UPSTREAM_READ_TIMEOUT_MS=FIVE_MINUTES,
            MITM_PROXY_PROXY_IDLE_TIMEOUT_MS="1000",
        ),
        connector=origin.connector,
    )
    assert proxy.pool.read_timeout == 300.0
    target = Target("example.org", 443, tls=True, sni="example.org")
    for protocol in ("http/1.1", "h2"):
        origin.protocol = protocol
        lease = await proxy.pool.acquire(target if protocol == "h2" else Target("example.net", 80))
        assert lease.connection.read_timeout == 300.0
    proxy.apply(
        make_config(
            tmp_path,
            MITM_PROXY_UPSTREAM_READ_TIMEOUT_MS="7000",
            MITM_PROXY_PROXY_IDLE_TIMEOUT_MS="1000",
        )
    )
    assert proxy.pool.read_timeout == 7.0
    # Connections already pooled read with the new value from now on, both protocols.
    pooled = [c for conns in proxy.pool._connections.values() for c in conns]
    assert {c.protocol for c in pooled} == {"http/1.1", "h2"}
    assert [c.read_timeout for c in pooled] == [7.0, 7.0]
    assert [c.window_timeout for c in pooled if isinstance(c, H2Upstream)] == [7.0]
    await proxy.stop()
    await asyncio.gather(*origin.tasks, return_exceptions=True)


async def test_the_client_side_and_upstream_writes_keep_the_idle_timeout(tmp_path: Path) -> None:
    env = {
        "MITM_PROXY_UPSTREAM_READ_TIMEOUT_MS": FIVE_MINUTES,
        "MITM_PROXY_PROXY_IDLE_TIMEOUT_MS": "1000",
    }
    proxy = ProxyServer(make_config(tmp_path, **env))
    assert isinstance(proxy.connector, TCPConnector)
    assert proxy.connector.write_timeout == 1.0
    assert proxy.client_write_timeout == 1.0
    assert proxy.source.idle_timeout == 1.0
    assert proxy._plain_context(("192.0.2.1", 5000)).idle_timeout == 1.0
    env["MITM_PROXY_PROXY_IDLE_TIMEOUT_MS"] = "2000"
    proxy.apply(make_config(tmp_path, **env))
    assert proxy.connector.write_timeout == 2.0
    assert proxy.client_write_timeout == 2.0
    assert proxy.pool.read_timeout == 300.0
    await proxy.stop()


def test_an_idle_intercepted_client_is_closed_on_the_idle_timeout_not_the_read_timeout(
    tmp_path: Path,
) -> None:
    config = make_config(
        tmp_path,
        MITM_PROXY_UPSTREAM_READ_TIMEOUT_MS=FIVE_MINUTES,
        MITM_PROXY_PROXY_IDLE_TIMEOUT_MS="1000",
    )

    async def scenario() -> float:
        proxy = ProxyServer(config, connector=Origin().connector)
        client_raw, proxy_raw = memory_pair()
        conn, _ = tunnel(proxy_raw)
        task = asyncio.create_task(proxy.handle_tunnel(conn))
        tls = TLSStream(
            client_raw,
            trusting_client_context(proxy.ca, ["http/1.1"]),
            server_side=False,
            server_hostname="example.org",
        )
        await tls.handshake()
        loop = asyncio.get_running_loop()
        started = loop.time()
        await task  # the proxy's H1 server ends the idle connection
        await proxy.stop()
        return loop.time() - started

    assert 1.0 <= run_virtual(scenario()) < 2.0


UPGRADE = (
    b"GET /ws HTTP/1.1\r\nHost: example.org\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n"
)


async def _upgrade(b: Bench, ctx: ExchangeContext, until: bytes) -> bytes:
    """Send a WebSocket upgrade through the proxy; the client's bytes once until arrives."""
    client_stream = b.proxy(ctx)
    await client_stream.write(UPGRADE)
    data = b""
    while until not in data:
        chunk = await client_stream.read()
        assert chunk, data
        data += chunk
    return data


def test_the_websocket_upgrade_reads_with_it_and_a_silent_origin_is_a_504(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The origin answers the upgrade after 300.1 s; the read timeout is five minutes, the
    # client idle timeout longer. Reading with the idle timeout, the 403 would come through.
    caplog.set_level(logging.INFO, logger="mitm_proxy.flows")

    async def scenario() -> tuple[bytes, float]:
        b = bench(Origin(websocket=False, delay=300.1, status=403, body=b"no"))
        b.pool.read_timeout = 300.0
        ctx = b.context("http/1.1", access_log=True)
        ctx.idle_timeout = 3600
        loop = asyncio.get_running_loop()
        started = loop.time()
        data = await _upgrade(b, ctx, b"in time\n")
        took = loop.time() - started
        while not any(r.name == "mitm_proxy.flows" for r in caplog.records):
            await asyncio.sleep(0.01)  # a virtual wait: the run's limit still applies
        await b.close()
        return data, took

    data, took = run_virtual(scenario())
    # The same answer as any other origin read that times out (design 0012 section 5).
    assert data.startswith(b"HTTP/1.1 504")
    assert data.endswith(b"the origin did not answer in time\n")
    assert b"server-timing" not in data.lower()  # never on an upgrade
    assert 300.0 <= took < 300.1
    line = next(r.getMessage() for r in caplog.records if r.name == "mitm_proxy.flows")
    assert " GET https://example.org/ws 504 " in line
    assert line.endswith(" outcome=error:504")


@pytest.mark.parametrize(
    ("error", "status"),
    [(UpstreamConnectError("refused"), 502), (UpstreamTimeout("connect timed out"), 504)],
)
async def test_a_websocket_upgrade_that_cannot_connect_keeps_its_status(
    error: UpstreamError, status: int
) -> None:
    origin = Origin(websocket=True)
    origin.fail_connect = error
    b = bench(origin)
    data = await _upgrade(b, b.context("http/1.1"), str(error).encode() + b"\n")
    assert data.startswith(f"HTTP/1.1 {status}".encode())
    await b.close()


async def _through_the_proxy(
    tmp_path: Path, protocol: str, silent_for: float, read_timeout_ms: str
) -> tuple[Reply, float]:
    """One request over intercepted TLS to an origin that says nothing for silent_for seconds."""
    origin = Origin(protocol=protocol, delay=silent_for)
    config = make_config(
        tmp_path,
        MITM_PROXY_UPSTREAM_READ_TIMEOUT_MS=read_timeout_ms,
        MITM_PROXY_PROXY_IDLE_TIMEOUT_MS=TWO_MINUTES,
    )
    proxy = ProxyServer(config, connector=origin.connector)
    client_raw, proxy_raw = memory_pair()
    conn, _ = tunnel(proxy_raw)
    task = asyncio.create_task(proxy.handle_tunnel(conn))
    tls = TLSStream(
        client_raw,
        trusting_client_context(proxy.ca, [protocol]),
        server_side=False,
        server_hostname="example.org",
    )
    await tls.handshake()
    assert tls.alpn == protocol
    client: H1Upstream | H2Upstream
    if protocol == "h2":
        client = H2Upstream(tls, read_timeout=3600)
        await client.start()
    else:
        client = H1Upstream(tls, read_timeout=3600)
    loop = asyncio.get_running_loop()
    started = loop.time()
    reply = await send(client, path="/v1/messages")
    took = loop.time() - started
    await client.close()
    await asyncio.wait_for(task, 5)
    await proxy.stop()
    await asyncio.gather(*origin.tasks, return_exceptions=True)
    return reply, took


@pytest.mark.parametrize("protocol", ["http/1.1", "h2"])
def test_a_five_minute_silent_origin_succeeds_with_a_five_minute_read_timeout(
    tmp_path: Path, protocol: str
) -> None:
    # Five minutes of silence, just under the limit, on a two-minute client idle timeout: the
    # client side doesn't end the open request (h1 isn't reading; h2 has a stream open).
    reply, took = run_virtual(_through_the_proxy(tmp_path, protocol, 299.9, FIVE_MINUTES))
    assert (reply.status, reply.body) == (200, b"hello from origin")
    assert took >= 299.9


@pytest.mark.parametrize("protocol", ["http/1.1", "h2"])
@pytest.mark.parametrize(
    ("silent_for", "read_timeout_ms"),
    [(300.1, FIVE_MINUTES), (120.1, TWO_MINUTES)],
)
def test_an_origin_silent_past_the_read_timeout_is_a_504(
    tmp_path: Path, protocol: str, silent_for: float, read_timeout_ms: str
) -> None:
    reply, took = run_virtual(_through_the_proxy(tmp_path, protocol, silent_for, read_timeout_ms))
    assert reply.status == 504
    assert b"did not answer in time" in reply.body
    assert int(read_timeout_ms) / 1000 <= took < silent_for
