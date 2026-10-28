"""Regression tests for the code-review findings on the initial implementation."""

import asyncio

import pytest

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
