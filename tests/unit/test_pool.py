import asyncio
import ssl
from typing import Any

import pytest

from mitm_proxy.errors import StartupError, UpstreamConnectError, UpstreamTimeout, UpstreamTLSError
from mitm_proxy.http.h1 import H1Upstream
from mitm_proxy.tls.upstream import ALPN_H1, UpstreamContexts, upstream_context
from mitm_proxy.upstream.pool import Target, TCPConnector
from tests.harness import Origin, bench, client_for, send

TARGET = Target("example.org", 443, tls=True, sni="example.org")


async def test_waits_for_a_free_h1_connection_then_times_out() -> None:
    b = bench(Origin(), max_per_host=1, connect_timeout=0.1)
    lease = await b.pool.acquire(TARGET)
    with pytest.raises(UpstreamTimeout, match="no free connection"):
        await b.pool.acquire(TARGET)
    assert isinstance(lease.connection, H1Upstream)
    lease.connection.busy = False
    await b.pool._done(lease.connection, True)
    again = await b.pool.acquire(TARGET)
    assert again.connection is lease.connection and again.reused
    await b.close()


async def test_known_h1_targets_open_connections_in_parallel() -> None:
    b = bench(Origin(), max_per_host=3)
    first = await b.pool.acquire(TARGET)
    second, third = await asyncio.gather(b.pool.acquire(TARGET), b.pool.acquire(TARGET))
    assert len({id(first.connection), id(second.connection), id(third.connection)}) == 3
    assert b.pool.counts()["http/1.1"] == 3
    await b.close()


async def test_idle_connections_are_swept() -> None:
    b = bench(Origin(), idle_timeout=0.01)
    client = await client_for(b.proxy(b.context("http/1.1")), "http/1.1")
    await send(client)
    assert b.pool.counts()["http/1.1"] == 1
    await asyncio.sleep(0.02)
    await b.pool.sweep_once()
    assert b.pool.counts()["http/1.1"] == 0
    await b.close()


async def test_closed_pool_refuses() -> None:
    b = bench()
    await b.pool.close()
    with pytest.raises(UpstreamConnectError, match="shutting down"):
        await b.pool.acquire(TARGET)


async def test_failed_connect_frees_the_slot() -> None:
    origin = Origin()
    origin.fail_connect = UpstreamConnectError("refused")
    b = bench(origin, max_per_host=1)
    for _ in range(2):
        with pytest.raises(UpstreamConnectError):
            await b.pool.acquire(TARGET)
    await b.close()


def test_upstream_context_always_verifies(tmp_path: Any) -> None:
    ctx = upstream_context()
    assert ctx.verify_mode is ssl.CERT_REQUIRED and ctx.check_hostname
    contexts = UpstreamContexts()
    assert contexts.for_alpn(ALPN_H1) is contexts.h1
    bad = tmp_path / "bad.pem"
    bad.write_text("not a certificate")
    with pytest.raises(StartupError, match=r"upstream\.extra_ca_file"):
        upstream_context(str(bad))


@pytest.mark.parametrize(
    ("raised", "expected", "message"),
    [
        (TimeoutError(), UpstreamTimeout, "timed out"),
        (ConnectionRefusedError(111, "Connection refused"), UpstreamConnectError, "refused"),
        (ssl.SSLError(1, "handshake failure"), UpstreamTLSError, "TLS handshake failed"),
    ],
)
async def test_tcp_connector_maps_errors(
    monkeypatch: pytest.MonkeyPatch, raised: Exception, expected: type[Exception], message: str
) -> None:
    async def fake_open(*args: object, **kwargs: object) -> None:
        raise raised

    monkeypatch.setattr(asyncio, "open_connection", fake_open)
    with pytest.raises(expected, match=message):
        await TCPConnector(UpstreamContexts())(TARGET, ("h2",), 1)


async def test_tcp_connector_reports_verification_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    error = ssl.SSLCertVerificationError(1, "verify failed")
    error.verify_message = "self-signed certificate"

    async def fake_open(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(asyncio, "open_connection", fake_open)
    with pytest.raises(UpstreamTLSError, match="certificate verification failed: self-signed"):
        await TCPConnector(UpstreamContexts())(TARGET, ("h2",), 1)
