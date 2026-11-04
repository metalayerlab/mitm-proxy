"""Observe-only middleware and the streaming path (design 0012, section 4)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import pytest

from mitm_proxy.errors import StartupError
from mitm_proxy.exchange import _observed
from mitm_proxy.http.body import StreamingBody
from mitm_proxy.http.flow import ClientConn, Flow, FlowType, Request, Response
from mitm_proxy.http.h1 import H1Upstream
from mitm_proxy.http.h2 import H2Upstream
from mitm_proxy.http.headers import Headers
from mitm_proxy.pipeline import Action, Middleware, Pipeline, is_mutation_capable, load_middleware
from mitm_proxy.upstream.pool import Connected, Target
from tests.harness import Origin, Reply, bench, client_for, send
from tests.helpers import MemoryStream, memory_pair

PAIRS = [("http/1.1", "http/1.1"), ("http/1.1", "h2"), ("h2", "http/1.1"), ("h2", "h2")]


def make_flow() -> Flow:
    return Flow(
        type=FlowType.HTTP,
        protocol="http/1.1",
        client_conn=ClientConn(("192.0.2.1", 50000)),
        request=Request("POST", "https", "example.org", "/"),
    )


class Observer(Middleware):
    """Observe-only: records every chunk call, and what read_all() did on the heads."""

    observe_only = True

    def __init__(self) -> None:
        self.request_chunks: list[tuple[bytes, bool]] = []
        self.response_chunks: list[tuple[bytes, bool]] = []
        self.read_all: list[str] = []
        self.flows: list[Flow] = []
        self._done = asyncio.Event()

    async def done(self) -> None:
        """Wait for on_done: a client can have the whole body before the last chunk call."""
        await asyncio.wait_for(self._done.wait(), 5)

    async def on_done(self, flow: Flow) -> None:
        self.flows.append(flow)
        self._done.set()

    async def _try_read(self, body: object) -> None:
        try:
            await body.read_all()  # type: ignore[attr-defined]  # a BodyStream
        except RuntimeError:
            self.read_all.append("raised")
        else:
            self.read_all.append("read")

    async def on_request(self, flow: Flow) -> Action:
        await self._try_read(flow.request.body)
        return Action.CONTINUE

    async def on_response(self, flow: Flow) -> Action:
        assert flow.response is not None
        await self._try_read(flow.response.body)
        flow.response.headers.set("x-observed", "1")  # header changes still work on the head
        return Action.CONTINUE

    async def on_request_chunk(self, flow: Flow, chunk: bytes, is_last: bool) -> None:
        self.request_chunks.append((chunk, is_last))

    async def on_response_chunk(self, flow: Flow, chunk: bytes, is_last: bool) -> None:
        self.response_chunks.append((chunk, is_last))


class Upper(Middleware):
    """Mutation-capable: forces the flow to buffer."""

    async def on_response_chunk(self, flow: Flow, chunk: bytes, is_last: bool) -> bytes | None:
        return chunk.upper()

    async def on_request_chunk(self, flow: Flow, chunk: bytes, is_last: bool) -> bytes | None:
        return chunk + b"+mutated"


class HeadOnly(Middleware):
    """Overrides no chunk hook, but isn't observe-only: it still buffers (the record's letter)."""

    async def on_request(self, flow: Flow) -> Action:
        return Action.CONTINUE


class Rewriter(Middleware):
    """Claims to observe, but returns bytes: an InvalidResult, skipped for the rest of the flow."""

    observe_only = True

    def __init__(self) -> None:
        self.calls = 0

    async def on_response_chunk(self, flow: Flow, chunk: bytes, is_last: bool) -> bytes | None:
        self.calls += 1
        return chunk.upper()


class FailClosedObserver(Middleware):
    observe_only = True
    on_error = "fail"


class NotABool(Middleware):
    observe_only = 1  # type: ignore[assignment]  # the mistake under test


def checked(chunks: list[tuple[bytes, bool]]) -> bytes:
    """The chunks joined, checking the calls' shape: is_last only on the last, which is empty."""
    assert chunks, "no chunk hook calls"
    assert [last for _, last in chunks] == [False] * (len(chunks) - 1) + [True]
    assert chunks[-1] == (b"", True)
    return b"".join(chunk for chunk, _ in chunks)


async def pieces(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part
        await asyncio.sleep(0.001)


async def send_in_parts(
    client: H1Upstream | H2Upstream,
    parts: list[bytes],
    *,
    chunked: bool = False,
) -> Reply:
    """A POST whose body goes in several parts: with a length, or (h1) chunked."""
    headers = Headers()
    if client.protocol == "http/1.1":
        if chunked:
            headers.set("transfer-encoding", "chunked")
        else:
            headers.set("content-length", str(sum(map(len, parts))))
    request = Request("POST", "https", "example.org", "/", headers)
    response = await client.request(
        request, headers, has_body=True, body=pieces(*parts), trailers=lambda: None
    )
    data = b"".join([chunk async for chunk in response.body])
    return Reply(response.response.status, response.response.headers, data, response.trailers())


# 1. The flag.


def test_observe_only_with_on_error_fail_is_refused_at_registration() -> None:
    with pytest.raises(StartupError, match="observe_only can't be combined with on_error"):
        Pipeline([("closed", FailClosedObserver())])
    ref = f"{__name__}:FailClosedObserver"
    with pytest.raises(StartupError, match="observe_only can't be combined with on_error"):
        load_middleware({"closed": ref}, ["closed"])


def test_observe_only_must_be_a_bool() -> None:
    with pytest.raises(ValueError, match="observe_only must be True or False"):
        Pipeline([("odd", NotABool())])
    with pytest.raises(StartupError, match="observe_only must be True or False"):
        load_middleware({"odd": f"{__name__}:NotABool"}, ["odd"])


def test_an_observe_only_middleware_is_not_mutation_capable() -> None:
    assert not is_mutation_capable(Observer())
    assert is_mutation_capable(Upper())
    assert Middleware.observe_only is False


def test_a_flow_streams_only_when_every_applying_middleware_observes() -> None:
    class Elsewhere(Upper):
        def applies_to(self, flow: Flow) -> bool:
            return False

    alone = Pipeline([("watch", Observer())]).start(make_flow())
    assert alone.streams and not alone.mutation_capable
    mixed = Pipeline([("watch", Observer()), ("upper", Upper())]).start(make_flow())
    assert not mixed.streams and mixed.mutation_capable
    heads = Pipeline([("watch", Observer()), ("head", HeadOnly())]).start(make_flow())
    assert not heads.streams and not heads.mutation_capable
    # A middleware that doesn't apply to the flow doesn't count.
    elsewhere = Pipeline([("watch", Observer()), ("upper", Elsewhere())]).start(make_flow())
    assert elsewhere.streams
    assert not Pipeline([]).start(make_flow()).streams


# 2. When a flow streams.


async def test_each_chunk_is_written_onward_before_the_hooks_see_it() -> None:
    events: list[str] = []

    async def observe(chunk: bytes, is_last: bool) -> None:
        events.append(f"observed {chunk.decode() or '-'}{' last' if is_last else ''}")

    async for chunk in _observed(pieces(b"a", b"", b"b"), observe):
        events.append(f"sent {chunk.decode()}")
    assert events == ["sent a", "observed a", "sent b", "observed b", "observed - last"]


@pytest.mark.parametrize(("client_proto", "origin_proto"), PAIRS)
async def test_a_streaming_flow_buffers_neither_body(client_proto: str, origin_proto: str) -> None:
    body = b"0123456789" * 20
    b = bench(Origin(protocol=origin_proto, body=body, trickle=0.001))
    watch = Observer()
    ctx = b.context(client_proto, [("watch", watch)])
    client = await client_for(b.proxy(ctx), client_proto)
    reply = await send_in_parts(client, [b"one,", b"two,", b"three"])
    await watch.done()
    assert reply.status == 200 and reply.body == body
    assert reply.headers.get("x-observed") == "1"
    assert b.origin.seen[0].body == b"one,two,three"
    assert checked(watch.request_chunks) == b"one,two,three"
    response = checked(watch.response_chunks)
    assert response == body and len(watch.response_chunks) > 2  # chunk by chunk, not one
    # On the heads, before the bodies: read_all() refuses on a streaming flow.
    assert watch.read_all == ["raised", "raised"]
    # Not mutation-capable: the lengths go through as declared.
    if client_proto == origin_proto == "http/1.1":
        assert b.origin.seen[0].raw_headers.get("content-length") == "13"
    if isinstance(client, H1Upstream):
        assert client.last_raw_headers.get("content-length") == str(len(body))
    await b.close()


@pytest.mark.parametrize(("client_proto", "origin_proto"), PAIRS)
async def test_bodies_of_unknown_length_stream(client_proto: str, origin_proto: str) -> None:
    # h1: chunked both ways; h2: DATA frames with no content-length.
    body = b"abcdefghij" * 5
    b = bench(Origin(protocol=origin_proto, body=body, chunked=True, trickle=0.001))
    watch = Observer()
    client = await client_for(b.proxy(b.context(client_proto, [("watch", watch)])), client_proto)
    reply = await send_in_parts(client, [b"x" * 7, b"y" * 9], chunked=True)
    await watch.done()
    assert reply.body == body
    assert checked(watch.request_chunks) == b"x" * 7 + b"y" * 9
    assert checked(watch.response_chunks) == body
    if isinstance(client, H1Upstream):
        assert client.last_raw_headers.get("transfer-encoding") == "chunked"
    await b.close()


async def test_a_close_delimited_response_streams() -> None:
    """An h1 origin that sends no length and ends the body by closing the connection."""
    origin = Origin()

    async def serve(stream: MemoryStream) -> None:
        received = b""
        while b"\r\n\r\n" not in received:
            received += await stream.read()
        await stream.write(b"HTTP/1.1 200 OK\r\ncontent-type: text/plain\r\n\r\n")
        for part in (b"first ", b"second ", b"third"):
            await stream.write(part)
            await asyncio.sleep(0.001)
        await stream.close()

    async def connector(target: Target, alpn: tuple[str, ...], timeout: float) -> Connected:
        proxy_end, origin_end = memory_pair()
        origin.tasks.append(asyncio.create_task(serve(origin_end)))
        return Connected(proxy_end, "http/1.1", ("192.0.2.80", target.port))

    b = bench(origin)
    b.pool.connector = connector  # a raw origin for this test
    watch = Observer()
    client = await client_for(b.proxy(b.context("http/1.1", [("watch", watch)])), "http/1.1")
    reply = await send(client)
    await watch.done()
    assert reply.body == b"first second third"
    assert checked(watch.response_chunks) == b"first second third"
    await b.close()


async def test_a_body_larger_than_the_buffer_limit_streams_with_its_hooks() -> None:
    # body.max_buffer_bytes applies only to buffered flows.
    body = b"z" * 1000
    b = bench(Origin(body=body, echo=True))
    watch = Observer()
    ctx = b.context("http/1.1", [("watch", watch)], max_buffer=10)
    client = await client_for(b.proxy(ctx), "http/1.1")
    reply = await send(client, "POST", body=body)
    await watch.done()
    assert reply.body == body
    assert checked(watch.request_chunks) == body and checked(watch.response_chunks) == body
    assert b.metrics.oversize_passthrough.values == {}
    await b.close()


async def test_deny_and_stop_work_on_a_streaming_flows_heads() -> None:
    class Gate(Observer):
        def __init__(self, phase: str, action: Action) -> None:
            super().__init__()
            self.phase, self.action = phase, action

        async def on_request(self, flow: Flow) -> Action:
            if self.action is Action.STOP:
                flow.response = Response.make(200, b"local")
            return self.action if self.phase == "request" else Action.CONTINUE

        async def on_response(self, flow: Flow) -> Action:
            return self.action if self.phase == "response" else Action.CONTINUE

    cases = [
        ("request", Action.DENY, 403, b"denied by middleware gate\n", 0),
        ("response", Action.DENY, 502, b"denied by middleware gate\n", 1),
        ("request", Action.STOP, 200, b"local", 0),
    ]
    for phase, action, status, body, reached in cases:
        b = bench(Origin())
        gate = Gate(phase, action)
        client = await client_for(b.proxy(b.context("h2", [("gate", gate)])), "h2")
        reply = await send(client, "POST", body=b"payload")
        await gate.done()
        assert (reply.status, reply.body) == (status, body)
        assert len(b.origin.seen) == reached
        assert gate.response_chunks == []  # the origin's body never went out
        await b.close()


# 3. Results.


async def test_an_observe_only_hook_that_returns_bytes_is_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = b"keep me exactly as I am" * 4
    b = bench(Origin(body=body, trickle=0.001))
    rewriter, watch = Rewriter(), Observer()
    ctx = b.context("http/1.1", [("rewriter", rewriter), ("watch", watch)])
    client = await client_for(b.proxy(ctx), "http/1.1")
    with caplog.at_level(logging.ERROR, logger="mitm_proxy.pipeline"):
        reply = await send(client)
        await watch.done()
    assert reply.status == 200 and reply.body == body  # the stream carried on untouched
    assert rewriter.calls == 1  # skipped for the rest of the flow (fail open)
    assert checked(watch.response_chunks) == body  # the others still see every chunk
    assert b.metrics.middleware_failures.values == {("rewriter",): 1.0}
    assert "it is observe-only: it must return None" in caplog.text
    assert "KEEP" not in caplog.text  # the type is named, never the value
    await b.close()


# 4. Mixed flows.


@pytest.mark.parametrize("client_proto", ["http/1.1", "h2"])
async def test_a_mixed_chain_buffers_and_observers_see_the_body_as_sent(
    client_proto: str,
) -> None:
    b = bench(Origin(echo=True, trickle=0.001))
    watch = Observer()
    ctx = b.context(client_proto, [("watch", watch), ("upper", Upper())])
    client = await client_for(b.proxy(ctx), client_proto)
    reply = await send_in_parts(client, [b"abc", b"def"])
    await watch.done()
    assert reply.body == b"ABCDEF+MUTATED"
    # One chunk each, with is_last, after the mutating middleware (as sent), though
    # "watch" comes first in the chain.
    assert watch.request_chunks == [(b"abcdef+mutated", True)]
    assert watch.response_chunks == [(b"ABCDEF+MUTATED", True)]
    assert watch.read_all == ["read", "read"]  # buffered: read_all() works
    await b.close()


async def test_a_head_only_middleware_that_does_not_observe_still_buffers() -> None:
    b = bench(Origin(body=b"x" * 100, trickle=0.001))
    watch = Observer()
    ctx = b.context("http/1.1", [("watch", watch), ("head", HeadOnly())], max_buffer=10)
    client = await client_for(b.proxy(ctx), "http/1.1")
    reply = await send(client)
    assert reply.body == b"x" * 100
    # Buffered, and over the limit: the hooks are skipped, as for any buffered flow.
    assert b.metrics.oversize_passthrough.values == {("response",): 1.0}
    assert watch.response_chunks == []
    await b.close()


# 6. Timings.


async def test_a_streamed_flows_timings_and_server_timing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    b = bench(Origin(body=b"y" * 100, trickle=0.02))  # the body takes about 0.2 s
    watch = Observer()
    ctx = b.context("http/1.1", [("watch", watch)], server_timing=True, access_log=True)
    client = await client_for(b.proxy(ctx), "http/1.1")
    with caplog.at_level(logging.INFO, logger="mitm_proxy.flows"):
        reply = await send(client)
        await watch.done()
    (flow,) = watch.flows
    t = flow.timings
    assert t.ttfb is not None and t.total is not None and t.complete is not None
    assert t.response_headers is not None and t.complete - t.response_headers >= 0.15
    # Server-Timing goes out with the head, before the body streams: mitm-proxy's duration is
    # the time to the head, not the whole flow (it is the whole flow on a buffered one).
    timing: dict[str, float] = {}
    for part in (reply.headers.get("server-timing") or "").split(","):
        name, _, rest = part.strip().partition(";")
        if rest.startswith("dur="):
            timing[name] = float(rest[4:].split(";")[0])
    proxy_ms = timing["mitm-proxy"]
    assert proxy_ms < t.total * 1000 - 100
    assert "mitm-origin" in timing and "mitm-mw" in timing
    assert "ttfb=" in caplog.text and "outcome=origin" in caplog.text
    await b.close()


def test_a_streaming_body_can_be_read_only_once() -> None:
    body = StreamingBody(pieces(b"a"))
    body.__aiter__()
    with pytest.raises(RuntimeError, match="only be iterated once"):
        body.__aiter__()
    assert not body.is_complete()
