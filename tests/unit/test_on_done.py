"""The on_done hook and flow.outcome (design 0006)."""

import asyncio

import pytest

from mitm_proxy.errors import UpstreamConnectError
from mitm_proxy.exchange import handle
from mitm_proxy.http.flow import Flow, Outcome, Request, Response
from mitm_proxy.http.messages import IncomingRequest
from mitm_proxy.pipeline import Action, Middleware, is_mutation_capable
from tests.harness import Origin, bench, client_for, send
from tests.unit.test_access_log import _no_body, _Stalled
from tests.unit.test_exchange import Upper


class Done(Middleware):
    """Records each flow it's told has ended, with what it could see then."""

    def __init__(self, calls: list[str] | None = None, tag: str = "done") -> None:
        self.calls = calls if calls is not None else []
        self.tag = tag
        self.seen: list[tuple[str, Outcome | None, bool]] = []

    async def on_done(self, flow: Flow) -> None:
        self.calls.append(self.tag)
        self.seen.append((flow.request.path, flow.outcome, flow.timings.complete is not None))


async def _told(done: Done, count: int = 1) -> None:
    """on_done runs just after the client has the last byte."""
    while len(done.seen) < count:
        await asyncio.sleep(0)


async def test_on_done_is_told_how_each_flow_ended() -> None:
    class Guard(Middleware):
        async def on_request(self, flow: Flow) -> Action:
            if flow.request.path == "/cached":
                flow.response = Response.make(200, b"cached")
                return Action.STOP
            if flow.request.path == "/blocked":
                return Action.DENY
            return Action.CONTINUE

    origin = Origin()
    b = bench(origin)
    done = Done()
    ctx = b.context("h2", [("done", done), ("guard", Guard())])
    client = await client_for(b.proxy(ctx), "h2")
    origin.fail_connect = UpstreamConnectError("refused")  # before any pooled connection
    await send(client, path="/down")
    origin.fail_connect = None
    await send(client, path="/fine")
    await send(client, path="/cached")
    await send(client, path="/blocked")
    await _told(done, 4)
    assert done.seen == [
        ("/down", Outcome("error", None, 502), True),
        ("/fine", Outcome("origin", None, 200), True),
        ("/cached", Outcome("local", "guard", 200), True),
        ("/blocked", Outcome("denied", "guard", 403), True),
    ]
    await b.close()


async def test_on_done_runs_in_order_once_and_only_where_middleware_applied() -> None:
    class Narrow(Done):
        def applies_to(self, flow: Flow) -> bool:
            return flow.request.path == "/narrow"

    calls: list[str] = []
    first, narrow, last = Done(calls, "first"), Narrow(calls, "narrow"), Done(calls, "last")
    b = bench()
    ctx = b.context("http/1.1", [("first", first), ("narrow", narrow), ("last", last)])
    client = await client_for(b.proxy(ctx), "http/1.1")
    await send(client, path="/wide")
    await send(client, path="/narrow")
    await _told(last, 2)
    assert calls == ["first", "last", "first", "narrow", "last"]
    await b.close()


async def test_on_done_reaches_middleware_that_skipped_an_oversize_body() -> None:
    done = Done()
    b = bench(Origin(echo=True))
    client = await client_for(b.proxy(b.context("h2", [("done", done)], max_buffer=2)), "h2")
    await send(client, "POST", "/big", body=b"more than two bytes")
    await _told(done)
    assert done.seen[0][1] == Outcome("origin", None, 200)
    await b.close()


async def test_a_failing_on_done_is_contained_and_counted() -> None:
    class Broken(Middleware):
        async def on_done(self, flow: Flow) -> None:
            raise RuntimeError("boom")

    done = Done()
    b = bench()
    ctx = b.context("http/1.1", [("broken", Broken()), ("done", done)])
    client = await client_for(b.proxy(ctx), "http/1.1")
    assert (await send(client)).status == 200
    assert (await send(client)).status == 200  # the connection carries on
    await _told(done, 2)
    assert b.metrics.middleware_failures.values == {("broken",): 2.0}
    await b.close()


async def test_middleware_that_failed_earlier_is_not_told() -> None:
    class Fails(Done):
        async def on_request(self, flow: Flow) -> Action:
            raise RuntimeError("boom")

    fails, done = Fails(), Done()
    b = bench()
    ctx = b.context("http/1.1", [("fails", fails), ("done", done)])
    client = await client_for(b.proxy(ctx), "http/1.1")
    await send(client)
    await _told(done)
    assert fails.seen == []
    await b.close()


async def test_on_done_runs_when_the_flow_is_cancelled_and_the_cancellation_resumes() -> None:
    class Slow(Done):
        async def on_done(self, flow: Flow) -> None:
            await asyncio.sleep(0.01)  # an await inside a cancelled task's cleanup
            await super().on_done(flow)

    done = Slow()
    b = bench()
    ctx = b.context("h2", [("done", done)])
    responder = _Stalled()
    incoming = IncomingRequest(
        Request("GET", "https", "example.org", "/slow"), _no_body(), has_body=False
    )
    task = asyncio.create_task(handle(ctx, incoming, responder))
    await responder.head.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert done.seen == [("/slow", Outcome("aborted", None, 200), True)]
    await b.close()


async def test_on_done_time_is_counted_and_the_flow_is_read_only_after() -> None:
    class Rewrites(Middleware):
        async def on_done(self, flow: Flow) -> None:
            await asyncio.sleep(0.02)
            flow.response = Response.make(500, b"too late")

    b = bench()
    seen: list[Flow] = []

    class Grab(Middleware):
        async def on_done(self, flow: Flow) -> None:
            seen.append(flow)

    ctx = b.context("http/1.1", [("late", Rewrites()), ("grab", Grab())])
    client = await client_for(b.proxy(ctx), "http/1.1")
    reply = await send(client)
    while not seen:
        await asyncio.sleep(0.005)
    assert (reply.status, reply.body) == (200, b"hello from origin")
    assert seen[0].timings.middleware["late"] >= 0.02
    with pytest.raises(AttributeError):
        seen[0].outcome = None  # type: ignore[misc]  # read-only: the assignment under test
    await b.close()


def test_on_done_alone_is_not_mutation_capable() -> None:
    assert not is_mutation_capable(Done())
    assert is_mutation_capable(Upper())


async def test_websocket_flows_never_reach_on_done() -> None:
    done = Done()
    b = bench(Origin(websocket=True))
    client_stream = b.proxy(b.context("http/1.1", [("done", done)]))
    await client_stream.write(
        b"GET /ws HTTP/1.1\r\nHost: example.org\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n"
        b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    head = b""
    while b"\r\n\r\n" not in head:
        head += await client_stream.read()
    await client_stream.close()
    await asyncio.sleep(0.01)
    assert done.seen == []
    await b.close()


async def test_a_cancelled_http2_connection_waits_for_its_streams_on_done() -> None:
    """Shutdown cancels connections after the grace period: their streams are still told."""

    class Writes(Done):
        async def on_done(self, flow: Flow) -> None:
            await asyncio.sleep(0.05)  # e.g. writing a record somewhere
            await super().on_done(flow)

    done = Writes()
    b = bench(Origin(delay=1))  # still waiting on the origin at shutdown
    client = await client_for(b.proxy(b.context("h2", [("done", done)])), "h2")
    pending = asyncio.create_task(send(client, path="/in-flight"))
    await asyncio.sleep(0.05)
    connection = b.tasks[-1]
    connection.cancel()
    await asyncio.gather(connection, return_exceptions=True)
    assert done.seen == [("/in-flight", Outcome("aborted", None, None), True)]
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)
    await b.close()
