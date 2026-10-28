"""Regression tests for the code-review findings on the initial implementation."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from mitm_proxy.errors import UpstreamConnectError
from mitm_proxy.exchange import handle
from mitm_proxy.http.flow import Flow, Request, Response
from mitm_proxy.http.h2 import H2Upstream
from mitm_proxy.http.headers import Headers
from mitm_proxy.http.messages import IncomingRequest
from mitm_proxy.pipeline import Middleware
from mitm_proxy.streams import PlainStream
from tests.harness import Origin, bench, client_for, send
from tests.unit.test_access_log import _no_body

BIG = b"x" * (700 * 1024)  # most of the 1 MiB HTTP/2 connection window


async def test_an_unread_upload_gives_its_window_back_to_the_connection() -> None:
    """Finding 1: DATA queued on a stream that ended early must be acknowledged."""
    origin = Origin(connect_delay=0.05)  # the upload is all queued before the 502
    origin.fail_connect = UpstreamConnectError("refused")
    b = bench(origin)
    client = await client_for(b.proxy(b.context("h2")), "h2")
    assert (await send(client, "POST", "/a", body=BIG)).status == 502
    # Without the acknowledgement only ~300 KiB of connection window is left, and this
    # upload waits for WINDOW_UPDATE frames that never come.
    reply = await asyncio.wait_for(send(client, "POST", "/b", body=BIG), 2)
    assert reply.status == 502
    await b.close()


class _GoneBeforeTheHead:
    """A client that goes away before the response head: no byte of the body is read."""

    protocol = "h2"
    started = False

    def __init__(self) -> None:
        self.waiting = asyncio.Event()

    async def start(self, response: Response, headers: Headers, *, end: bool) -> None:
        self.waiting.set()
        await asyncio.Event().wait()

    async def data(self, chunk: bytes) -> None:  # pragma: no cover - never reached
        pass

    async def end(self, trailers: Headers | None) -> None:  # pragma: no cover - never reached
        pass


async def test_a_discarded_response_gives_its_window_back_to_the_shared_upstream() -> None:
    """Finding 2: DATA queued on a dropped upstream stream must be acknowledged.

    Leaked window only shrinks the shared connection's window until it adds up to the
    whole window; then every flow on that connection stalls. A response the client
    never started reading leaks all of it at once.
    """
    b = bench(Origin(protocol="h2", body=b"y" * (1536 * 1024)))  # more than the window
    gone = _GoneBeforeTheHead()
    incoming = IncomingRequest(Request("GET", "https", "example.org", "/a"), _no_body(), False)
    task = asyncio.create_task(handle(b.context("h2"), incoming, gone))
    await gone.waiting.wait()
    await asyncio.sleep(0.05)  # the origin fills the whole window meanwhile
    task.cancel()  # the client goes away: the response is discarded unread
    await asyncio.gather(task, return_exceptions=True)
    client = await client_for(b.proxy(b.context("h2")), "h2")
    reply = await asyncio.wait_for(send(client, path="/b"), 2)  # same pooled connection
    assert len(reply.body) == 1536 * 1024
    assert b.origin.connections == 1
    await b.close()


def test_a_cached_leaf_is_replaced_before_it_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    """Finding 3: a proxy up for over 30 days must not present an expired leaf."""
    import datetime

    from mitm_proxy.tls import ca as ca_module
    from tests.helpers import make_ca

    start = datetime.datetime.now(datetime.UTC)
    clock = [start]
    monkeypatch.setattr(ca_module, "_now", lambda: clock[0])
    authority = make_ca()
    first = authority.server_context("example.org")
    clock[0] = start + datetime.timedelta(days=20)
    assert authority.server_context("example.org") is first  # still well inside 30 days
    clock[0] = start + datetime.timedelta(days=29, hours=12)
    renewed = authority.server_context("example.org")
    assert renewed is not first  # a day before expiry: a fresh leaf
    clock[0] = start + datetime.timedelta(days=40)
    assert authority.server_context("example.org") is renewed  # the new one lasts 30 days


@pytest.mark.parametrize("client_proto", ["http/1.1", "h2"])
async def test_a_response_hook_can_make_a_response_bodiless(client_proto: str) -> None:
    """Finding 4: framing follows the status middleware leaves, not the origin's."""
    from mitm_proxy.http.flow import Flow
    from mitm_proxy.pipeline import Action, Middleware

    class NotModified(Middleware):
        async def on_response(self, flow: Flow) -> Action:
            assert flow.response is not None
            flow.response.status = 304
            return Action.CONTINUE

    b = bench(Origin(body=b"a body a 304 must not carry"))
    ctx = b.context(client_proto, [("cache", NotModified())])
    client = await client_for(b.proxy(ctx), client_proto)
    first = await asyncio.wait_for(send(client), 2)
    second = await asyncio.wait_for(send(client), 2)  # the connection survived
    assert (first.status, first.body) == (304, b"")
    assert second.status == 304
    await b.close()


@pytest.mark.parametrize("client_proto", ["http/1.1", "h2"])
async def test_a_response_hook_can_give_a_bodiless_response_a_body(client_proto: str) -> None:
    """Finding 4: a 204 replaced by a 200 with a body keeps that body."""
    from mitm_proxy.http.flow import Flow
    from mitm_proxy.pipeline import Action, Middleware

    class Fill(Middleware):
        async def on_response(self, flow: Flow) -> Action:
            flow.response = Response.make(200, b"filled in")
            return Action.CONTINUE

    b = bench(Origin(status=204, body=b""))
    client = await client_for(b.proxy(b.context(client_proto, [("fill", Fill())])), client_proto)
    reply = await asyncio.wait_for(send(client), 2)
    assert (reply.status, reply.body) == (200, b"filled in")
    await b.close()


async def test_an_applies_to_failure_is_counted() -> None:
    """Finding 8: the failure metric must see errors raised in applies_to too."""
    from mitm_proxy.http.flow import Flow
    from mitm_proxy.pipeline import Middleware

    class Broken(Middleware):
        def applies_to(self, flow: Flow) -> bool:
            raise RuntimeError("boom")

    b = bench()
    client = await client_for(b.proxy(b.context("http/1.1", [("broken", Broken())])), "http/1.1")
    assert (await send(client)).status == 200  # skipped, and the flow carries on
    assert b.metrics.middleware_failures.values == {("broken",): 1.0}
    await b.close()


async def test_a_failed_websocket_switch_closes_the_upstream_socket() -> None:
    """Finding 7: if the client is gone when switching, the origin's socket is closed."""

    async def switch(reply: Headers) -> object:
        raise ConnectionResetError("the client went away")

    b = bench(Origin(websocket=True))
    incoming = IncomingRequest(
        Request("GET", "https", "example.org", "/ws"),
        _no_body(),
        False,
        websocket=switch,  # type: ignore[arg-type]  # raises before returning a stream
        raw_headers=Headers([("upgrade", "websocket")]),
    )
    with pytest.raises(ConnectionResetError):
        await handle(b.context("http/1.1"), incoming, _GoneBeforeTheHead())
    # The origin echoes until its socket closes: its task ends only if the proxy closed it.
    await asyncio.wait_for(asyncio.gather(*b.origin.tasks), 2)
    await b.close()


def _goaway(error_code: int) -> bytes:
    """A raw GOAWAY frame, written past the test client's own h2 state machine."""
    from hyperframe.frame import GoAwayFrame

    frame = GoAwayFrame(0)
    frame.last_stream_id = 1
    frame.error_code = error_code
    return bytes(frame.serialize())


async def test_streams_finish_after_a_graceful_goaway() -> None:
    """Finding 5: a graceful GOAWAY lets open streams complete (RFC 9113, 6.8).

    Also pins the override of h2's private GOAWAY handler: an h2 upgrade that changes
    it fails here.
    """
    big = b"z" * (3 * 1024 * 1024)  # needs WINDOW_UPDATEs read after the GOAWAY
    b = bench(Origin(body=big, delay=0.1))  # the whole download comes after the GOAWAY
    stream = b.proxy(b.context("h2"))
    client = await client_for(stream, "h2")
    download = asyncio.create_task(send(client, path="/big"))
    await asyncio.sleep(0.02)
    await stream.write(_goaway(0))
    reply = await asyncio.wait_for(download, 5)
    assert (reply.status, len(reply.body)) == (200, len(big))
    await asyncio.wait_for(b.tasks[-1], 2)  # and then the connection ends
    await b.close()


async def test_an_error_goaway_still_ends_the_connection_at_once() -> None:
    from mitm_proxy.errors import UpstreamError

    b = bench(Origin(body=b"z" * (3 * 1024 * 1024), delay=0.1))
    stream = b.proxy(b.context("h2"))
    client = await client_for(stream, "h2")
    download = asyncio.create_task(send(client, path="/big"))
    await asyncio.sleep(0.02)
    await stream.write(_goaway(2))  # INTERNAL_ERROR
    with pytest.raises(UpstreamError):
        await asyncio.wait_for(download, 5)
    await b.close()


IDLE = 0.2  # the proxy's idle timeout in these tests


class _Ended(Middleware):
    """Applies to every flow (so its request body is buffered) and records how each ended."""

    def __init__(self) -> None:
        self.outcomes: list[str] = []

    async def on_done(self, flow: Flow) -> None:
        self.outcomes.append(str(flow.outcome))


async def _keepalive(client: H2Upstream) -> None:
    """Connection-level traffic (PINGs), so only the stalled stream is idle."""
    while True:
        client.conn.ping(b"keepaliv")
        await client.flush()
        await asyncio.sleep(IDLE / 4)


async def test_a_silent_http2_connection_is_closed_even_with_a_stream_open() -> None:
    """Finding 6: proxy.idle_timeout_ms applies while streams are open, too."""
    b = bench()
    client = await client_for(b.proxy(b.context("h2", [("ended", _Ended())]), idle=IDLE), "h2")
    assert isinstance(client, H2Upstream)
    sid = client.conn.get_next_available_stream_id()
    head = [(b":method", b"POST"), (b":scheme", b"https"), (b":authority", b"example.org")]
    client.conn.send_headers(sid, [*head, (b":path", b"/")], end_stream=False)
    await client.flush()  # a request body is promised, and then nothing at all
    await asyncio.wait_for(b.tasks[-1], 2)  # the proxy gives up on the connection
    await b.close()


async def test_a_stalled_http2_request_body_ends_its_stream() -> None:
    """Finding 6: a request body that stops arriving ends its stream, not the connection."""
    ended = _Ended()
    b = bench()
    client = await client_for(b.proxy(b.context("h2", [("ended", ended)]), idle=IDLE), "h2")
    assert isinstance(client, H2Upstream)
    keepalive = asyncio.create_task(_keepalive(client))
    sid = client.conn.get_next_available_stream_id()
    head = [(b":method", b"POST"), (b":scheme", b"https"), (b":authority", b"example.org")]
    client.conn.send_headers(sid, [*head, (b":path", b"/")], end_stream=False)
    client.conn.send_data(sid, b"the start of a body that never finishes")
    await client.flush()
    for _ in range(100):
        if ended.outcomes:
            break
        await asyncio.sleep(IDLE / 4)
    assert ended.outcomes == ["aborted"]
    assert (await send(client)).status == 200  # the connection itself is still fine
    keepalive.cancel()
    await b.close()


async def test_a_client_that_never_reads_ends_its_stream() -> None:
    """Finding 6: waiting for a client's flow-control window is bounded too."""
    ended = _Ended()
    b = bench(Origin(body=b"w" * (2 * 1024 * 1024)))  # more than the client's window
    client = await client_for(b.proxy(b.context("h2", [("ended", ended)]), idle=IDLE), "h2")
    assert isinstance(client, H2Upstream)
    keepalive = asyncio.create_task(_keepalive(client))
    request = Request("GET", "https", "example.org", "/never-read")
    response = await client.request(  # the body is never read: no window goes back
        request, Headers(), has_body=False, body=_no_body(), trailers=lambda: None
    )
    assert response.response.status == 200
    for _ in range(100):
        if ended.outcomes:
            break
        await asyncio.sleep(IDLE / 4)
    assert ended.outcomes == ["aborted"]
    keepalive.cancel()
    await b.close()


# Second review of the fixes above.


async def test_a_trickling_response_survives_a_graceful_goaway() -> None:
    """R1: after a GOAWAY the client may go quiet while a response is still flowing."""
    b = bench(Origin(body=b"t" * 1000, trickle=IDLE / 4))  # ~2.5 idle periods, no acks due
    stream = b.proxy(b.context("h2"), idle=IDLE)
    client = await client_for(stream, "h2")
    download = asyncio.create_task(send(client, path="/slow"))
    await asyncio.sleep(0.02)
    await stream.write(_goaway(0))
    reply = await asyncio.wait_for(download, 5)
    assert (reply.status, len(reply.body)) == (200, 1000)
    await b.close()


async def test_a_slow_origin_gets_its_time_on_an_idle_http2_connection() -> None:
    """R2: the connection's idle close must not pre-empt an open stream's own timeouts."""
    b = bench(Origin(delay=IDLE * 2))  # the origin thinks; nothing moves either way
    client = await client_for(b.proxy(b.context("h2"), idle=IDLE), "h2")
    reply = await asyncio.wait_for(send(client), 5)
    assert reply.status == 200
    await b.close()


async def test_a_stalled_upload_is_the_clients_fault_not_the_origins(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R3: a client that stops uploading ends the flow as aborted, not as an origin error."""
    import logging

    caplog.set_level(logging.INFO, logger="mitm_proxy.flows")

    async def stalls() -> AsyncIterator[bytes]:
        yield b"the start of an upload"
        await asyncio.Event().wait()

    b = bench(Origin(echo=True))
    client = await client_for(b.proxy(b.context("h2", access_log=True), idle=IDLE), "h2")
    request = Request("POST", "https", "example.org", "/upload")
    upload = asyncio.create_task(
        client.request(request, Headers(), has_body=True, body=stalls(), trailers=lambda: None)
    )
    for _ in range(100):
        lines = [r.getMessage() for r in caplog.records if r.name == "mitm_proxy.flows"]
        if lines:
            break
        await asyncio.sleep(IDLE / 4)
    assert lines and lines[0].endswith(" outcome=aborted"), lines
    messages = [r.getMessage() for r in caplog.records]
    blamed = [m for m in messages if "connection to the origin" in m]
    assert blamed == []  # not logged (or counted) as an origin failure
    upload.cancel()
    await asyncio.gather(upload, return_exceptions=True)
    await b.close()


async def test_cancelling_a_draining_connection_leaves_no_tasks_behind() -> None:
    """R4: the GOAWAY drain's helper tasks are cleaned up when the connection is cancelled."""
    b = bench(Origin(delay=5))  # a stream stays open: the connection is draining
    stream = b.proxy(b.context("h2"))
    client = await client_for(stream, "h2")
    pending = asyncio.create_task(send(client))
    await asyncio.sleep(0.02)
    await stream.write(_goaway(0))
    await asyncio.sleep(0.05)
    connection = b.tasks[-1]
    connection.cancel()
    await asyncio.gather(connection, return_exceptions=True)
    await asyncio.sleep(0)
    helpers = ("Event.wait", "MemoryStream.read")  # what the drain waits on
    leftovers = [
        t for t in asyncio.all_tasks() if not t.done() and any(h in repr(t) for h in helpers)
    ]
    assert leftovers == []
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)
    await b.close()


# Third review: socket writes had no bound.


async def _socket_pair() -> tuple[PlainStream, asyncio.StreamWriter, PlainStream]:
    """A connected local socket pair (no network): our stream, and a peer that never reads."""
    import socket

    ours, theirs = socket.socketpair()
    for sock in (ours, theirs):  # small buffers: a few writes fill them
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    reader, writer = await asyncio.open_connection(sock=ours)
    peer_reader, peer_writer = await asyncio.open_connection(sock=theirs)
    return (
        PlainStream(reader, writer, write_timeout=IDLE),
        peer_writer,
        PlainStream(peer_reader, peer_writer),
    )


async def test_a_write_to_a_peer_that_stopped_reading_fails_after_the_timeout() -> None:
    """Third review: a socket write must not wait forever (an origin or client that hangs)."""
    stream, peer_writer, _ = await _socket_pair()
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(ConnectionResetError, match="stopped reading"):
        for _ in range(10_000):  # far more than the socket buffers hold
            await asyncio.wait_for(stream.write(b"x" * 65536), 5)
    assert loop.time() - started < 2
    assert stream.is_closing()  # aborted: the stream is unusable after a timed-out write
    peer_writer.close()


async def test_a_write_to_a_reading_peer_is_not_cut_off() -> None:
    stream, peer_writer, peer = await _socket_pair()

    async def drain_slowly() -> int:
        received = 0
        while chunk := await peer.read():
            received += len(chunk)
            await asyncio.sleep(IDLE / 10)  # slower than the writer, but steady
        return received

    reader = asyncio.create_task(drain_slowly())
    for _ in range(20):
        await stream.write(b"y" * 8192)
    await stream.close()
    assert await asyncio.wait_for(reader, 10) == 20 * 8192
    peer_writer.close()


def test_both_ends_get_the_write_timeout(tmp_path: Path) -> None:
    from mitm_proxy.server import ProxyServer
    from mitm_proxy.upstream.pool import TCPConnector
    from tests.unit.test_server import make_config

    proxy = ProxyServer(make_config(tmp_path, MITM_PROXY_PROXY_IDLE_TIMEOUT_MS="7000"))
    assert isinstance(proxy.connector, TCPConnector)
    assert proxy.connector.write_timeout == 7.0  # origins
    assert proxy.client_write_timeout == 7.0  # clients


# Fourth review: the write bound itself.


async def test_closing_with_unsent_data_to_a_peer_that_never_reads_is_bounded() -> None:
    """F1: close() waits for queued data to go out; a peer that never reads must not hang it."""
    stream, peer_writer, _ = await _socket_pair()
    stream.writer.write(b"q" * 400_000)  # more than the peer's reader buffers by itself
    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(stream.close(), 5)
    assert loop.time() - started < 2
    peer_writer.close()


async def test_close_does_not_swallow_cancellation() -> None:
    """F1: a cancelled close (shutdown) must stay cancelled, and leave the socket closed."""
    stream, peer_writer, _ = await _socket_pair()
    stream.write_timeout = 30  # longer than the test: only the cancellation can end it
    stream.writer.write(b"q" * 400_000)
    closing = asyncio.create_task(stream.close())
    await asyncio.sleep(0.05)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert stream.writer.transport.is_closing()
    peer_writer.close()


async def test_one_large_write_to_a_steady_reader_is_not_cut_off() -> None:
    """F2: the timeout is for no progress, not for the whole of a large write."""
    stream, peer_writer, peer = await _socket_pair()
    size = 400_000  # takes several timeout periods at the reader's pace

    async def read_steadily() -> int:
        received = 0
        while received < size:
            chunk = await peer.read(4096)  # about 400 KB/s: steady, but slow
            if not chunk:
                break
            received += len(chunk)
            await asyncio.sleep(0.01)
        return received

    reader = asyncio.create_task(read_steadily())
    started = asyncio.get_running_loop().time()
    await stream.write(b"L" * size)  # one write, as a buffered body is
    assert asyncio.get_running_loop().time() - started > IDLE  # it did take a while
    assert await asyncio.wait_for(reader, 10) == size
    await stream.close()
    peer_writer.close()


@pytest.mark.parametrize("write_timeout", [None, IDLE])
async def test_a_real_socket_timeout_is_not_mistaken_for_ours(write_timeout: float | None) -> None:
    """F3: the OS's ETIMEDOUT (an OSError) passes through as itself."""
    import errno

    stream, peer_writer, _ = await _socket_pair()
    stream.write_timeout = write_timeout

    async def dead_connection() -> None:
        raise TimeoutError(errno.ETIMEDOUT, "Connection timed out")

    stream.writer.drain = dead_connection  # type: ignore[method-assign]  # the failure under test
    with pytest.raises(TimeoutError) as raised:
        await stream.write(b"x")
    assert raised.value.errno == errno.ETIMEDOUT
    peer_writer.close()


def test_the_origin_write_timeout_follows_a_config_change(tmp_path: Path) -> None:
    """F4: like the client side, the origin side uses the current proxy.idle_timeout_ms."""
    from mitm_proxy.server import ProxyServer
    from mitm_proxy.upstream.pool import TCPConnector
    from tests.unit.test_server import make_config

    proxy = ProxyServer(make_config(tmp_path, MITM_PROXY_PROXY_IDLE_TIMEOUT_MS="7000"))
    proxy.apply(make_config(tmp_path, MITM_PROXY_PROXY_IDLE_TIMEOUT_MS="9000"))
    assert isinstance(proxy.connector, TCPConnector)
    assert (proxy.connector.write_timeout, proxy.client_write_timeout) == (9.0, 9.0)
