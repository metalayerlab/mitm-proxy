"""The proxy server's tunnel handling and the explicit-proxy source, in memory."""

import asyncio
import logging
from pathlib import Path

import pytest

from mitm_proxy.config import Config, load_config
from mitm_proxy.errors import StartupError, UpstreamConnectError
from mitm_proxy.http.h1 import H1Upstream
from mitm_proxy.http.h2 import H2Upstream
from mitm_proxy.interception.base import InterceptedConnection
from mitm_proxy.server import ProxyServer, _is_loopback, config_hash
from mitm_proxy.streams import ByteStream
from mitm_proxy.tls.terminate import TLSStream
from tests.harness import Origin, send
from tests.helpers import memory_pair, trusting_client_context


def make_config(tmp_path: Path, **env: str) -> Config:
    environ = {
        "MITM_PROXY_STATE_DIR": str(tmp_path),
        "MITM_PROXY_INTERCEPT_HOSTS": "example.org,*.example.org",
        "MITM_PROXY_PROXY_PEEK_TIMEOUT_MS": "50",
        **env,
    }
    return load_config(tmp_path / "none.yaml", environ=environ)


def tunnel(
    stream: ByteStream, host: str = "example.org", port: int = 443
) -> tuple[InterceptedConnection, list[str]]:
    events: list[str] = []

    async def accept() -> None:
        events.append("accept")

    async def reject(status: int, message: str) -> None:
        events.append(f"reject {status} {message}")

    return InterceptedConnection(stream, host, port, ("192.0.2.1", 5000), accept, reject), events


@pytest.mark.parametrize("alpn", ["h2", "http/1.1"])
async def test_intercepts_tls_end_to_end(tmp_path: Path, alpn: str) -> None:
    origin = Origin(protocol="h2")
    proxy = ProxyServer(make_config(tmp_path), connector=origin.connector)
    client_raw, proxy_raw = memory_pair()
    conn, events = tunnel(proxy_raw)
    task = asyncio.create_task(proxy.handle_tunnel(conn))
    tls = TLSStream(
        client_raw,
        trusting_client_context(proxy.ca, [alpn]),
        server_side=False,
        server_hostname="example.org",
    )
    await tls.handshake()
    assert tls.alpn == alpn
    client: H1Upstream | H2Upstream
    if alpn == "h2":
        client = H2Upstream(tls, read_timeout=5)
        await client.start()
    else:
        client = H1Upstream(tls, read_timeout=5)
    reply = await send(client, path="/inside")
    assert (reply.status, reply.body) == (200, b"hello from origin")
    assert events == ["accept"]
    assert origin.seen[0].path == "/inside"
    assert proxy.metrics.flows.values == {("http",): 1.0}
    await client.close()
    await asyncio.wait_for(task, 2)
    await proxy.stop()


async def relay_http_through(
    proxy: ProxyServer, host: str, first: bytes = b""
) -> tuple[bytes, list[str]]:
    client, proxy_raw = memory_pair()
    conn, events = tunnel(proxy_raw, host, 80)
    task = asyncio.create_task(proxy.handle_tunnel(conn))
    if first:
        await client.write(first)
    await client.write(b"GET /raw HTTP/1.1\r\nHost: example.net\r\n\r\n")
    data = b""
    while b"origin" not in data:
        chunk = await client.read()
        if not chunk:
            break
        data += chunk
    await client.close()
    await asyncio.wait_for(task, 2)
    return data, events


async def test_out_of_scope_is_relayed_without_peeking(tmp_path: Path) -> None:
    origin = Origin()
    proxy = ProxyServer(make_config(tmp_path), connector=origin.connector)
    data, events = await relay_http_through(proxy, "example.net")
    assert data.startswith(b"HTTP/1.1 200")
    assert events == ["accept"]
    assert proxy.metrics.tunnels.values == {("out_of_scope",): 1.0}
    await proxy.stop()


async def test_in_scope_but_not_tls_is_relayed_with_the_peeked_bytes(tmp_path: Path) -> None:
    origin = Origin()
    proxy = ProxyServer(make_config(tmp_path), connector=origin.connector)
    data, _ = await relay_http_through(proxy, "example.org")
    assert data.startswith(b"HTTP/1.1 200")
    assert origin.seen[0].path == "/raw"
    assert proxy.metrics.tunnels.values == {("not_tls",): 1.0}
    await proxy.stop()


async def test_server_speaks_first_protocols_are_relayed_after_the_peek_timeout(
    tmp_path: Path,
) -> None:
    proxy = ProxyServer(make_config(tmp_path), connector=Origin().connector)
    client, proxy_raw = memory_pair()
    conn, _ = tunnel(proxy_raw)
    task = asyncio.create_task(proxy.handle_tunnel(conn))
    await asyncio.sleep(0.1)  # longer than the 50 ms peek timeout, sending nothing
    assert proxy.metrics.tunnels.values == {("not_tls",): 1.0}
    await client.close()
    await asyncio.wait_for(task, 2)
    await proxy.stop()


async def test_unlisted_sni_is_relayed(tmp_path: Path) -> None:
    proxy = ProxyServer(make_config(tmp_path), connector=Origin().connector)
    client_raw, proxy_raw = memory_pair()
    conn, _ = tunnel(proxy_raw)
    task = asyncio.create_task(proxy.handle_tunnel(conn))
    tls = TLSStream(
        client_raw,
        trusting_client_context(proxy.ca),
        server_side=False,
        server_hostname="example.net",
    )
    handshake = asyncio.create_task(tls.handshake())
    while not proxy.metrics.tunnels.values:
        await asyncio.sleep(0.01)
    assert proxy.metrics.tunnels.values == {("sni_out_of_scope",): 1.0}
    handshake.cancel()
    await client_raw.close()
    await asyncio.gather(handshake, return_exceptions=True)
    await asyncio.wait_for(task, 2)
    await proxy.stop()


async def test_untrusting_client_is_logged_and_closed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from tests.helpers import make_ca

    caplog.set_level(logging.INFO)
    proxy = ProxyServer(make_config(tmp_path), connector=Origin().connector)
    client_raw, proxy_raw = memory_pair()
    conn, _ = tunnel(proxy_raw)
    task = asyncio.create_task(proxy.handle_tunnel(conn))
    tls = TLSStream(
        client_raw,
        trusting_client_context(make_ca("other")),
        server_side=False,
        server_hostname="example.org",
    )
    with pytest.raises((OSError, ConnectionError)):
        await tls.handshake()
    await asyncio.wait_for(task, 2)
    assert "install the root CA" in caplog.text
    await proxy.stop()


async def test_relay_connect_failure_rejects_the_connect(tmp_path: Path) -> None:
    origin = Origin()
    origin.fail_connect = UpstreamConnectError("refused")
    proxy = ProxyServer(make_config(tmp_path), connector=origin.connector)
    _client, proxy_raw = memory_pair()
    conn, events = tunnel(proxy_raw, "example.net")
    await proxy.handle_tunnel(conn)
    assert events == ["reject 502 refused"]
    await proxy.stop()


@pytest.mark.parametrize(
    ("host", "reason"), [("example.net", "out_of_scope"), ("example.org", "not_tls")]
)
async def test_relayed_tunnels_are_logged_when_the_user_asks(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, host: str, reason: str
) -> None:
    caplog.set_level(logging.INFO, logger="mitm_proxy.relayed")
    origin = Origin()
    config = make_config(tmp_path, MITM_PROXY_PROXY_ACCESS_LOG_RELAYED="true")
    proxy = ProxyServer(config, connector=origin.connector)
    data, _ = await relay_http_through(proxy, host)
    line = next(r.getMessage() for r in caplog.records if r.name == "mitm_proxy.relayed")
    assert line.startswith("tunnel ")
    assert f" 192.0.2.1:5000 {host}:80 {reason} up=" in line
    up = int(line.split(" up=")[1].split()[0])
    down = int(line.split(" down=")[1].split()[0])
    request = b"GET /raw HTTP/1.1\r\nHost: example.net\r\n\r\n"
    assert (up, down) == (len(request), len(data))  # peeked bytes are counted too
    await proxy.stop()


async def test_relayed_tunnels_are_not_logged_by_default(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="mitm_proxy.relayed")
    proxy = ProxyServer(make_config(tmp_path), connector=Origin().connector)
    await relay_http_through(proxy, "example.net")
    assert not [r for r in caplog.records if r.name == "mitm_proxy.relayed"]
    await proxy.stop()


async def test_a_tunnel_that_cannot_connect_is_logged_with_its_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="mitm_proxy.relayed")
    origin = Origin()
    origin.fail_connect = UpstreamConnectError("refused")
    config = make_config(tmp_path, MITM_PROXY_PROXY_ACCESS_LOG_RELAYED="true")
    proxy = ProxyServer(config, connector=origin.connector)
    _client, proxy_raw = memory_pair()
    conn, _ = tunnel(proxy_raw, "example.net")
    await proxy.handle_tunnel(conn)
    line = next(r.getMessage() for r in caplog.records if r.name == "mitm_proxy.relayed")
    assert " example.net:443 error:502 up=0 down=0 " in line
    await proxy.stop()


async def test_explicit_source_end_to_end(tmp_path: Path) -> None:
    origin = Origin()
    proxy = ProxyServer(make_config(tmp_path), connector=origin.connector)
    client_raw, proxy_raw = memory_pair()
    task = asyncio.create_task(proxy.source.handle_client(proxy_raw, ("192.0.2.1", 5000)))
    await client_raw.write(b"CONNECT example.org:443 HTTP/1.1\r\nHost: example.org:443\r\n\r\n")
    head = await client_raw.read()
    assert head.startswith(b"HTTP/1.1 200")
    tls = TLSStream(
        client_raw,
        trusting_client_context(proxy.ca, ["http/1.1"]),
        server_side=False,
        server_hostname="example.org",
    )
    await tls.handshake()
    client = H1Upstream(tls, read_timeout=5)
    assert (await send(client)).status == 200
    await client.close()
    await asyncio.wait_for(task, 2)
    await proxy.stop()


async def test_plain_http_through_the_source(tmp_path: Path) -> None:
    origin = Origin()
    proxy = ProxyServer(make_config(tmp_path), connector=origin.connector)
    client_raw, proxy_raw = memory_pair()
    task = asyncio.create_task(proxy.source.handle_client(proxy_raw, ("192.0.2.1", 5000)))
    client = H1Upstream(client_raw, read_timeout=5)
    reply = await send(client, path="http://example.net/plain", authority="example.net")
    assert reply.status == 200 and origin.seen[0].path == "/plain"
    assert proxy.metrics.flows.values == {("plain_http",): 1.0}
    await client.close()
    await asyncio.wait_for(task, 2)
    await proxy.stop()


async def test_status_apply_and_hash(tmp_path: Path) -> None:
    config = make_config(tmp_path, MITM_PROXY_PROXY_AUTH="u:p")
    proxy = ProxyServer(config, connector=Origin().connector)
    status = proxy.status()
    assert status["state"] == "starting" and status["mode"] == "standalone"
    assert status["config"]["hash"] == config_hash(config)
    assert "u:p" not in str(status) and "example.org" not in str(status)
    same_but_other_secret = make_config(tmp_path, MITM_PROXY_PROXY_AUTH="u:other")
    assert config_hash(same_but_other_secret) == config_hash(config)
    changed = make_config(tmp_path, MITM_PROXY_INTERCEPT_HOSTS="example.net")
    proxy.apply(changed)
    assert proxy.snapshot.config is changed
    assert proxy.metrics.config_applies.values == {("applied",): 2.0}
    await proxy.stop()


async def test_start_failure_is_a_startup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(*args: object, **kwargs: object) -> None:
        raise OSError(98, "Address already in use")

    monkeypatch.setattr(asyncio, "start_server", refuse)
    proxy = ProxyServer(make_config(tmp_path), connector=Origin().connector)
    with pytest.raises(StartupError, match="Address already in use"):
        await proxy.start()
    await proxy.stop()


def test_is_loopback() -> None:
    assert _is_loopback("127.0.0.1") and _is_loopback("::1") and _is_loopback("localhost")
    assert not _is_loopback("0.0.0.0") and not _is_loopback("example.org")  # noqa: S104 - a test value
