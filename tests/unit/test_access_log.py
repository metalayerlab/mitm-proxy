"""The built-in access log (design 0005): one line per flow, written by the exchange."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable

import pytest

from mitm_proxy.errors import UpstreamConnectError
from mitm_proxy.exchange import handle
from mitm_proxy.http.flow import Flow, Request, Response
from mitm_proxy.http.headers import Headers
from mitm_proxy.http.messages import IncomingRequest
from mitm_proxy.pipeline import Action, Middleware
from mitm_proxy.scope import Allowlist
from tests.harness import Origin, bench, client_for, send
from tests.unit.test_exchange import PAIRS, Upper

Lines = Callable[[], list[str]]


@pytest.fixture
def lines(caplog: pytest.LogCaptureFixture) -> Lines:
    """The access-log lines written so far."""
    caplog.set_level(logging.INFO, logger="mitm_proxy.flows")
    caplog.set_level(logging.INFO, logger="mitm_proxy.relayed")

    def written() -> list[str]:
        loggers = ("mitm_proxy.flows", "mitm_proxy.relayed")
        return [r.getMessage() for r in caplog.records if r.name in loggers]

    return written


async def _logged(lines: Lines, count: int = 1) -> list[str]:
    """The lines, once count are written: a line follows the client's last byte."""
    while len(lines()) < count:
        await asyncio.sleep(0)
    return lines()


@pytest.mark.parametrize(("client_proto", "origin_proto"), PAIRS)
async def test_an_origin_response_is_logged_with_counted_bytes(
    lines: Lines, client_proto: str, origin_proto: str
) -> None:
    b = bench(Origin(protocol=origin_proto, echo=True))
    client = await client_for(b.proxy(b.context(client_proto, access_log=True)), client_proto)
    await send(client, "POST", "/submit", body=b"payload")
    line = (await _logged(lines))[0]
    legs = f"{client_proto}>{origin_proto}"
    assert f" {legs} POST https://example.org/submit 200 req=7 resp=7 " in line
    assert " connect=" in line and " ttfb=" in line and "total=-" not in line
    assert line.endswith(" outcome=origin")
    await b.close()


async def test_a_chunk_hook_rewrite_is_logged_at_its_new_size(lines: Lines) -> None:
    b = bench(Origin(echo=True))
    ctx = b.context("http/1.1", [("upper", Upper())], access_log=True)
    client = await client_for(b.proxy(ctx), "http/1.1")
    reply = await send(client, "POST", "/", body=b"abc")
    line = (await _logged(lines))[0]
    assert reply.body == b"ABC+MUTATED"
    assert " 200 req=3 resp=11 " in line
    await b.close()


async def test_local_answers_denies_and_errors_are_logged(lines: Lines) -> None:
    class Guard(Middleware):
        async def on_request(self, flow: Flow) -> Action:
            if flow.request.path == "/cached":
                flow.response = Response.make(200, b"cached")
                return Action.STOP
            if flow.request.path == "/blocked":
                return Action.DENY
            if flow.request.path == "/broken":
                return Action.STOP
            return Action.CONTINUE

    origin = Origin()
    b = bench(origin)
    ctx = b.context("http/1.1", [("guard", Guard())], access_log=True)
    client = await client_for(b.proxy(ctx), "http/1.1")
    await send(client, path="/cached")
    await send(client, path="/blocked")
    await send(client, path="/broken")
    origin.fail_connect = UpstreamConnectError("refused")
    await send(client, path="/down")
    cached, blocked, broken, down = (await _logged(lines, 4))[:4]
    assert "org/cached 200 req=0 resp=6 " in cached and cached.endswith(" outcome=local:guard")
    assert " connect=- ttfb=- " in cached
    assert "org/blocked 403 " in blocked and blocked.endswith(" outcome=denied:guard")
    assert "org/broken 502 " in broken and broken.endswith(" outcome=error:502")
    assert "org/down 502 " in down and down.endswith(" outcome=error:502")
    await b.close()


async def test_the_log_is_off_by_default(lines: Lines) -> None:
    b = bench()
    client = await client_for(b.proxy(b.context("h2")), "h2")
    await send(client)
    await asyncio.sleep(0.01)
    assert lines() == []
    await b.close()


class _Stalled:
    """A client that takes the response head, then never reads the body."""

    protocol = "http/1.1"

    def __init__(self) -> None:
        self.started = False
        self.head = asyncio.Event()

    async def start(self, response: Response, headers: Headers, *, end: bool) -> None:
        self.started = True
        self.head.set()

    async def data(self, chunk: bytes) -> None:
        await asyncio.Event().wait()

    async def end(self, trailers: Headers | None) -> None:
        pass


async def _no_body() -> AsyncIterator[bytes]:
    return
    yield b""


async def test_a_cancelled_flow_is_logged_as_aborted(lines: Lines) -> None:
    b = bench()
    ctx = b.context("h2", access_log=True)
    responder = _Stalled()
    incoming = IncomingRequest(
        Request("GET", "https", "example.org", "/slow"), _no_body(), has_body=False
    )
    task = asyncio.create_task(handle(ctx, incoming, responder))
    await responder.head.wait()
    task.cancel()  # as an HTTP/2 stream reset does
    with pytest.raises(asyncio.CancelledError):
        await task
    line = lines()[0]
    assert " GET https://example.org/slow 200 req=0 resp=0 " in line
    assert line.endswith(" outcome=aborted")
    await b.close()


async def test_a_websocket_is_logged_when_it_closes(lines: Lines) -> None:
    b = bench(Origin(websocket=True))
    client_stream = b.proxy(b.context("http/1.1", access_log=True))
    await client_stream.write(
        b"GET /ws HTTP/1.1\r\nHost: example.org\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n"
        b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    head = b""
    while b"\r\n\r\n" not in head:
        head += await client_stream.read()
    await client_stream.write(b"frames")
    assert await client_stream.read() == b"FRAMES"
    await client_stream.close()
    line = (await _logged(lines))[0]
    assert " GET https://example.org/ws 101 req=6 resp=6 " in line
    assert line.endswith(" outcome=websocket")
    await b.close()


async def test_relayed_plain_http_has_its_own_setting_and_logger(
    caplog: pytest.LogCaptureFixture, lines: Lines
) -> None:
    b = bench(Origin(echo=True))
    ctx = b.context(
        "http/1.1",
        target=None,
        scope=Allowlist(["example.org"]),
        access_log_relayed=True,
    )
    client = await client_for(b.proxy(ctx), "http/1.1")
    await send(client, "POST", "http://example.org/a", body=b"in")  # intercepted: access_log off
    await send(client, "POST", "http://example.net/b", body=b"out", authority="example.net")
    await _logged(lines)
    await asyncio.sleep(0.01)
    assert len(lines()) == 1
    record = next(r for r in caplog.records if r.name == "mitm_proxy.relayed")
    line = record.getMessage()
    assert line.startswith("http ")
    assert " 192.0.2.1:50000 POST http://example.net/b 200 req=3 resp=3 total=" in line
    await b.close()
