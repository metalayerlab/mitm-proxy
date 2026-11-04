"""Chaining through exits (design 0013, section 2): the two connectors, against fake exits on
loopback. Nothing is resolved but the exits' own host, and nothing leaves the machine."""

from __future__ import annotations

import asyncio
import base64
import socket
from pathlib import Path
from typing import Any

import pytest
from interpose.secret import Secret

from mitm_proxy.errors import UpstreamConnectError, UpstreamTimeout, UpstreamTLSError
from mitm_proxy.http.body import empty
from mitm_proxy.http.flow import Request
from mitm_proxy.http.h1 import H1Upstream
from mitm_proxy.http.headers import Headers
from mitm_proxy.routing import Exit
from mitm_proxy.tls.upstream import ALPN_ANY, ALPN_H1, UpstreamContexts
from mitm_proxy.upstream.chain import (
    HTTPConnectConnector,
    RoutingConnector,
    Socks5Connector,
)
from mitm_proxy.upstream.pool import ConnectionPool, Target, forward_proxy
from tests.exits import TEST_LOGIN, FakeHTTPExit, FakeSocksExit, TestOrigin
from tests.harness import Origin, bench
from tests.helpers import make_ca

ROUTED = "routed.example"  # a name nothing here may resolve


def http_exit(port: int, *, login: str = "") -> Exit:
    return Exit("exit-a", "http", "127.0.0.1", port, country="XA", auth=Secret(login))


def socks_exit(port: int, *, login: str = "") -> Exit:
    return Exit("exit-tor", "socks5h", "127.0.0.1", port, auth=Secret(login))


def contexts(tmp_path: Path, ca_pem: bytes | None = None) -> UpstreamContexts:
    if ca_pem is None:
        return UpstreamContexts()
    path = tmp_path / "origin-ca.pem"
    path.write_bytes(ca_pem)
    return UpstreamContexts(str(path))


async def get(conn: H1Upstream, path: str = "/", authority: str = ROUTED) -> tuple[int, bytes]:
    request = Request("GET", "http", authority, path, Headers())
    response = await conn.request(
        request, Headers(), has_body=False, body=empty(), trailers=lambda: None
    )
    body = b"".join([chunk async for chunk in response.body])
    return response.response.status, body


class Resolutions:
    """Every name the event loop or the socket module was asked to resolve."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.names: list[str] = []
        loop = asyncio.get_running_loop()
        real_loop, real_socket = loop.getaddrinfo, socket.getaddrinfo

        async def loop_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
            self.names.append(str(host))
            return await real_loop(host, *args, **kwargs)

        def socket_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
            self.names.append(str(host))
            return real_socket(host, *args, **kwargs)

        monkeypatch.setattr(loop, "getaddrinfo", loop_getaddrinfo)
        monkeypatch.setattr(socket, "getaddrinfo", socket_getaddrinfo)


# HTTP CONNECT


async def test_http_connect_then_verified_tls_to_the_origin_inside_the_tunnel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ca = make_ca()
    origin = TestOrigin(ca=ca, host=ROUTED)
    exit_ = FakeHTTPExit(routes={f"{ROUTED}:443": await origin.start()})
    resolved = Resolutions(monkeypatch)
    connector = HTTPConnectConnector(contexts(tmp_path, ca.cert_pem))
    target = Target(ROUTED, 443, tls=True, sni=ROUTED, via=http_exit(await exit_.start()))
    connected = await connector(target, ALPN_H1, 5)
    assert connected.alpn == "http/1.1"
    assert connected.address is None  # the origin's address is the exit's to know
    status, body = await get(H1Upstream(connected.stream, read_timeout=5), "/inside")
    assert (status, body) == (200, b"hello from the origin")
    assert exit_.heads[0].startswith(f"CONNECT {ROUTED}:443 HTTP/1.1\r\n")
    assert f"Host: {ROUTED}:443\r\n" in exit_.heads[0]
    assert "proxy-authorization" not in exit_.heads[0].lower()
    assert origin.requests == ["GET /inside HTTP/1.1"]  # origin form, inside the tunnel
    assert ROUTED not in resolved.names
    await connected.stream.close()
    await exit_.close()
    await origin.close()


async def test_the_origin_certificate_is_verified_inside_the_tunnel(tmp_path: Path) -> None:
    origin = TestOrigin(ca=make_ca(), host=ROUTED)
    exit_ = FakeHTTPExit(routes={f"{ROUTED}:443": await origin.start()})
    connector = HTTPConnectConnector(contexts(tmp_path))  # the test CA isn't trusted
    target = Target(ROUTED, 443, tls=True, via=http_exit(await exit_.start()))
    with pytest.raises(UpstreamTLSError, match="certificate verification failed"):
        await connector(target, ALPN_H1, 5)
    await exit_.close()
    await origin.close()


async def test_http_connect_sends_the_exits_login(tmp_path: Path) -> None:
    origin = TestOrigin()
    exit_ = FakeHTTPExit(routes={f"{ROUTED}:80": await origin.start()}, login=TEST_LOGIN)
    connector = HTTPConnectConnector(contexts(tmp_path))
    via = http_exit(await exit_.start(), login=TEST_LOGIN)
    connected = await connector(Target(ROUTED, 80, tls=False, via=via, tunnel=True), (), 5)
    token = base64.b64encode(TEST_LOGIN.encode()).decode()
    assert f"Proxy-Authorization: Basic {token}\r\n" in exit_.heads[0]
    assert await get(H1Upstream(connected.stream, read_timeout=5)) == (
        200,
        b"hello from the origin",
    )
    await connected.stream.close()
    await exit_.close()
    await origin.close()


async def test_an_exit_that_wants_a_login_is_a_clean_upstream_error(tmp_path: Path) -> None:
    exit_ = FakeHTTPExit(login=TEST_LOGIN)
    connector = HTTPConnectConnector(contexts(tmp_path))
    target = Target(ROUTED, 443, tls=True, via=http_exit(await exit_.start()))
    with pytest.raises(UpstreamConnectError, match=r"exit exit-a refused CONNECT.*407.*login"):
        await connector(target, ALPN_ANY, 5)
    await exit_.close()


async def test_a_wrong_login_is_refused_without_naming_it(tmp_path: Path) -> None:
    exit_ = FakeHTTPExit(login=TEST_LOGIN)
    connector = HTTPConnectConnector(contexts(tmp_path))
    via = http_exit(await exit_.start(), login="tester:wrong-made-up")
    with pytest.raises(UpstreamConnectError, match="login refused") as caught:
        await connector(Target(ROUTED, 443, tls=True, via=via), ALPN_ANY, 5)
    assert "wrong-made-up" not in str(caught.value)
    await exit_.close()


@pytest.mark.parametrize("status", [403, 502])
async def test_a_non_2xx_answer_is_a_clean_upstream_error(tmp_path: Path, status: int) -> None:
    exit_ = FakeHTTPExit(status=status)
    connector = HTTPConnectConnector(contexts(tmp_path))
    target = Target(ROUTED, 443, tls=True, via=http_exit(await exit_.start()))
    with pytest.raises(UpstreamConnectError, match=f"refused CONNECT to {ROUTED}:443: {status}"):
        await connector(target, ALPN_ANY, 5)
    await exit_.close()


async def test_an_exit_that_cannot_be_reached_fails_the_connection(tmp_path: Path) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])  # closed again before it is used: nothing listens
    connector = HTTPConnectConnector(contexts(tmp_path))
    with pytest.raises(UpstreamConnectError, match="cannot connect to exit exit-a"):
        await connector(Target(ROUTED, 443, via=http_exit(port)), ALPN_ANY, 5)


async def test_a_silent_exit_times_out(tmp_path: Path) -> None:
    exit_ = FakeHTTPExit(silent=True)
    connector = HTTPConnectConnector(contexts(tmp_path))
    target = Target(ROUTED, 443, via=http_exit(await exit_.start()))
    with pytest.raises(UpstreamTimeout, match="through exit exit-a timed out"):
        await connector(target, ALPN_ANY, 0.2)
    await exit_.close()


async def test_plain_http_goes_to_an_http_exit_in_absolute_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exit_ = FakeHTTPExit()
    via = http_exit(await exit_.start(), login=TEST_LOGIN)
    resolved = Resolutions(monkeypatch)
    pool = ConnectionPool(HTTPConnectConnector(contexts(tmp_path)), read_timeout=5)
    lease = await pool.acquire(Target(ROUTED, 80, tls=False, via=via))
    assert isinstance(lease.connection, H1Upstream)
    status, body = await get(lease.connection, "/page?q=1")
    assert (status, body) == (200, f"exit saw GET http://{ROUTED}/page?q=1".encode())
    token = base64.b64encode(TEST_LOGIN.encode()).decode()
    assert f"proxy-authorization: Basic {token}\r\n" in exit_.heads[0]
    assert not exit_.heads[0].startswith("CONNECT")
    assert ROUTED not in resolved.names
    await pool.close()
    await exit_.close()


def test_only_plain_http_through_an_http_exit_uses_absolute_form() -> None:
    http, socks = http_exit(1), socks_exit(1)
    assert forward_proxy(Target(ROUTED, 80, tls=False, via=http)) is not None
    assert forward_proxy(Target(ROUTED, 80, tls=False, via=http, tunnel=True)) is None
    assert forward_proxy(Target(ROUTED, 443, tls=True, via=http)) is None
    assert forward_proxy(Target(ROUTED, 80, tls=False, via=socks)) is None
    assert forward_proxy(Target(ROUTED, 80, tls=False)) is None


async def test_an_ipv6_literal_is_bracketed_in_connect(tmp_path: Path) -> None:
    exit_ = FakeHTTPExit(status=403)
    connector = HTTPConnectConnector(contexts(tmp_path))
    with pytest.raises(UpstreamConnectError):
        await connector(Target("2001:db8::1", 443, via=http_exit(await exit_.start())), (), 5)
    assert exit_.heads[0].startswith("CONNECT [2001:db8::1]:443 HTTP/1.1\r\n")
    await exit_.close()


async def test_a_host_that_cannot_go_on_the_wire_is_refused(tmp_path: Path) -> None:
    connector = HTTPConnectConnector(contexts(tmp_path))
    with pytest.raises(UpstreamConnectError, match="not a host name"):
        await connector(Target("bad host\r\nx: y", 443, via=http_exit(1)), (), 5)


# SOCKS5


async def test_socks5_connects_by_name_so_the_exit_resolves_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = TestOrigin()
    exit_ = FakeSocksExit(routes={f"{ROUTED}:80": await origin.start()})
    resolved = Resolutions(monkeypatch)
    connector = Socks5Connector(contexts(tmp_path))
    via = socks_exit(await exit_.start())
    connected = await connector(Target(ROUTED, 80, tls=False, via=via), (), 5)
    assert exit_.methods == [b"\x00"]  # no authentication offered without a login
    assert [(r.address_type, r.host, r.port) for r in exit_.requests] == [(3, ROUTED, 80)]
    assert await get(H1Upstream(connected.stream, read_timeout=5), "/s") == (
        200,
        b"hello from the origin",
    )
    assert origin.requests == ["GET /s HTTP/1.1"]  # origin form: SOCKS is a plain tunnel
    assert ROUTED not in resolved.names
    await connected.stream.close()
    await exit_.close()
    await origin.close()


async def test_socks5_then_verified_tls_inside(tmp_path: Path) -> None:
    ca = make_ca()
    origin = TestOrigin(ca=ca, host=ROUTED)
    exit_ = FakeSocksExit(routes={f"{ROUTED}:443": await origin.start()})
    connector = Socks5Connector(contexts(tmp_path, ca.cert_pem))
    via = socks_exit(await exit_.start())
    connected = await connector(Target(ROUTED, 443, tls=True, via=via), ALPN_H1, 5)
    assert await get(H1Upstream(connected.stream, read_timeout=5)) == (
        200,
        b"hello from the origin",
    )
    await connected.stream.close()
    await exit_.close()
    await origin.close()


async def test_socks5_with_a_login(tmp_path: Path) -> None:
    origin = TestOrigin()
    exit_ = FakeSocksExit(routes={f"{ROUTED}:80": await origin.start()}, login=TEST_LOGIN)
    connector = Socks5Connector(contexts(tmp_path))
    via = socks_exit(await exit_.start(), login=TEST_LOGIN)
    connected = await connector(Target(ROUTED, 80, tls=False, via=via), (), 5)
    assert exit_.methods == [b"\x02"]
    assert exit_.logins == [tuple(TEST_LOGIN.split(":", 1))]
    await connected.stream.close()
    await exit_.close()
    await origin.close()


async def test_socks5_refusing_the_login_is_a_clean_upstream_error(tmp_path: Path) -> None:
    exit_ = FakeSocksExit(login=TEST_LOGIN)
    connector = Socks5Connector(contexts(tmp_path))
    via = socks_exit(await exit_.start(), login="tester:wrong-made-up")
    with pytest.raises(UpstreamConnectError, match="refused the SOCKS5 login") as caught:
        await connector(Target(ROUTED, 80, tls=False, via=via), (), 5)
    assert "wrong-made-up" not in str(caught.value)
    await exit_.close()


async def test_socks5_wanting_a_login_we_dont_have(tmp_path: Path) -> None:
    exit_ = FakeSocksExit(login=TEST_LOGIN)
    connector = Socks5Connector(contexts(tmp_path))
    via = socks_exit(await exit_.start())
    with pytest.raises(UpstreamConnectError, match="refused the SOCKS5 authentication"):
        await connector(Target(ROUTED, 80, tls=False, via=via), (), 5)
    await exit_.close()


@pytest.mark.parametrize(
    ("reply", "meaning"), [(1, "general SOCKS server failure"), (5, "connection refused")]
)
async def test_a_socks5_failure_is_a_clean_upstream_error(
    tmp_path: Path, reply: int, meaning: str
) -> None:
    exit_ = FakeSocksExit(reply=reply)
    connector = Socks5Connector(contexts(tmp_path))
    via = socks_exit(await exit_.start())
    with pytest.raises(UpstreamConnectError, match=f"exit exit-tor refused CONNECT.*{meaning}"):
        await connector(Target(ROUTED, 443, tls=True, via=via), ALPN_ANY, 5)
    await exit_.close()


@pytest.mark.parametrize(
    ("host", "address_type"), [("192.0.2.10", 1), ("2001:db8::1", 4), ("[2001:db8::1]", 4)]
)
async def test_socks5_passes_ip_literals_as_they_are(
    tmp_path: Path, host: str, address_type: int
) -> None:
    exit_ = FakeSocksExit(reply=5)  # refuse, after recording the request
    connector = Socks5Connector(contexts(tmp_path))
    via = socks_exit(await exit_.start())
    with pytest.raises(UpstreamConnectError):
        await connector(Target(host, 80, tls=False, via=via), (), 5)
    assert exit_.requests[0].address_type == address_type
    assert exit_.requests[0].host == host.strip("[]")
    await exit_.close()


async def test_something_that_isnt_socks5_is_a_clean_upstream_error(tmp_path: Path) -> None:
    exit_ = FakeHTTPExit()  # answers a SOCKS greeting with nonsense, then closes
    connector = Socks5Connector(contexts(tmp_path))
    via = socks_exit(await exit_.start())
    with pytest.raises((UpstreamConnectError, UpstreamTimeout)):
        await connector(Target(ROUTED, 80, tls=False, via=via), (), 0.5)
    await exit_.close()


# Choosing the connector


async def test_the_routing_connector_picks_by_the_exits_kind(tmp_path: Path) -> None:
    connector = RoutingConnector(contexts(tmp_path), write_timeout=3)
    seen: list[str] = []

    def record(name: str) -> Any:
        async def call(target: Target, alpn: tuple[str, ...], timeout: float) -> Any:
            seen.append(name)
            raise UpstreamConnectError("stop here")

        return call

    connector.http = record("http")  # a stand-in that records
    connector.socks5 = record("socks5")  # likewise
    for via in (http_exit(1), socks_exit(1)):
        with pytest.raises(UpstreamConnectError):
            await connector(Target(ROUTED, 443, via=via), ALPN_ANY, 1)
    assert seen == ["http", "socks5"]


def test_the_write_timeout_reaches_every_path(tmp_path: Path) -> None:
    connector = RoutingConnector(contexts(tmp_path), write_timeout=3)
    assert (connector.http.write_timeout, connector.socks5.write_timeout) == (3, 3)
    connector.write_timeout = 4
    assert (connector.write_timeout, connector.http.write_timeout) == (4, 4)
    assert connector.socks5.write_timeout == 4


# The pool


async def test_the_pool_never_reuses_a_connection_through_another_exit() -> None:
    b = bench(Origin())
    exit_a, exit_b = http_exit(1), Exit("exit-b", "http", "127.0.0.1", 2, country="XB")

    def target(via: Exit | None) -> Target:
        return Target("example.org", 443, tls=True, sni="example.org", via=via)

    assert target(exit_a) != target(exit_b) != target(None)
    through_a = await b.pool.acquire(target(exit_a))
    assert isinstance(through_a.connection, H1Upstream)
    through_a.connection.busy = False
    await b.pool._done(through_a.connection, True)  # idle, and free for exit-a's next request
    through_b = await b.pool.acquire(target(exit_b))
    direct = await b.pool.acquire(target(None))
    assert through_b.connection is not through_a.connection
    assert direct.connection is not through_a.connection
    assert not through_b.reused and not direct.reused
    again = await b.pool.acquire(target(exit_a))
    assert again.connection is through_a.connection and again.reused
    assert b.pool.counts()["http/1.1"] == 3
    await b.close()
