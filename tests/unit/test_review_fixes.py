"""Regression tests for the code-review findings on the initial implementation."""

import asyncio

from mitm_proxy.errors import UpstreamConnectError
from mitm_proxy.exchange import handle
from mitm_proxy.http.flow import Request, Response
from mitm_proxy.http.headers import Headers
from mitm_proxy.http.messages import IncomingRequest
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
