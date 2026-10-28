"""One request/response exchange: the client leg, the middleware, the upstream leg.

The protocol layers hand over an IncomingRequest and a Responder; this module
builds the Flow, runs the pipeline, picks the framing, talks to the origin
through the pool, and answers. It never sees h11 or h2 objects, which is what
lets the two legs speak different protocols.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from mitm_proxy.errors import (
    UpstreamConnectError,
    UpstreamError,
    UpstreamTimeout,
)
from mitm_proxy.http import framing
from mitm_proxy.http.body import BufferedBodyStream, PassthroughBody, empty
from mitm_proxy.http.flow import ClientConn, Flow, FlowType, Request, Response, ServerConn
from mitm_proxy.http.h1 import H1Upstream
from mitm_proxy.http.headers import Headers
from mitm_proxy.http.messages import IncomingRequest, Responder, UpstreamResponse
from mitm_proxy.metrics import Metrics
from mitm_proxy.pipeline import Action, FlowPipeline, Pipeline
from mitm_proxy.relay import relay
from mitm_proxy.scope import Allowlist
from mitm_proxy.streams import ByteStream
from mitm_proxy.upstream.pool import ConnectionPool, Target

log = logging.getLogger(__name__)

IDEMPOTENT = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE", "TRACE"})


@dataclass
class ExchangeContext:
    """What one client connection's exchanges share."""

    pool: ConnectionPool
    metrics: Metrics
    client_conn: ClientConn
    protocol: str  # the client leg
    max_buffer: int
    idle_timeout: float
    pipeline: Pipeline | None = None  # None: nothing applies
    # Intercepted TLS: the fixed upstream. None: plain-HTTP proxying, routed per request.
    target: Target | None = None
    scope: Allowlist | None = None  # plain-HTTP proxying: which hosts get middleware

    def route(self, request: Request) -> tuple[Target, Pipeline | None, str]:
        if self.target is not None:
            return self.target, self.pipeline, FlowType.HTTP.value
        host, port = request.host, request.port
        intercepted = self.scope is not None and self.scope.match(host) is not None
        return Target(host, port, tls=False), self.pipeline if intercepted else None, "plain_http"


class _Body:
    """An upstream body iterator that remembers whether it was finished."""

    def __init__(self, source: AsyncIterator[bytes]) -> None:
        self.source = source
        self.finished = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.source:
            yield chunk
        self.finished = True


@dataclass
class _Buffered:
    """Chunks read so far. Kept as chunks so an overflowing body is never copied."""

    chunks: list[bytes]
    size: int
    fits: bool  # the whole body is here, within the limit

    def join(self) -> bytes:
        """The body as one bytes object; the chunks are released."""
        data = self.chunks[0] if len(self.chunks) == 1 else b"".join(self.chunks)
        self.chunks.clear()
        return data


async def _buffer(source: AsyncIterator[bytes], limit: int) -> _Buffered:
    """Read until the body ends or passes limit bytes."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in source:
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            return _Buffered(chunks, size, fits=False)
    return _Buffered(chunks, size, fits=True)


async def _chain(first: list[bytes], rest: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """The buffered chunks, each released once sent, then the rest of the body."""
    for i, chunk in enumerate(first):
        first[i] = b""
        if chunk:
            yield chunk
    first.clear()
    async for chunk in rest:
        yield chunk


async def send_simple(
    responder: Responder, status: int, message: str, *, head_only: bool = False
) -> None:
    """A short plain-text response from the proxy itself."""
    body = (message + "\n").encode()
    response = Response.make(status, body)
    response.headers.set("content-length", str(len(body)))
    await responder.start(response, response.headers, end=head_only)
    if not head_only:
        await responder.data(body)
        await responder.end(None)


async def handle(ctx: ExchangeContext, incoming: IncomingRequest, responder: Responder) -> None:
    """Serve one request. Errors become proxy responses; nothing propagates but I/O errors."""
    if incoming.websocket is not None:
        await _websocket(ctx, incoming, incoming.websocket, responder)
        return
    request = incoming.request
    target, pipeline, kind = ctx.route(request)
    flow = Flow(
        type=FlowType.HTTP, protocol=ctx.protocol, client_conn=ctx.client_conn, request=request
    )
    ctx.metrics.flows.inc(kind)
    try:
        await _exchange(ctx, flow, incoming, responder, target, pipeline)
    finally:
        flow.timings.complete = time.monotonic()


async def _exchange(
    ctx: ExchangeContext,
    flow: Flow,
    incoming: IncomingRequest,
    responder: Responder,
    target: Target,
    pipeline: Pipeline | None,
) -> None:
    request = flow.request
    run = pipeline.start(flow) if pipeline is not None else None
    if run is not None:
        run.on_failure = lambda name: ctx.metrics.middleware_failures.inc(name)
    active: FlowPipeline | None = run if run is not None and run.applies else None
    head_only = request.method == "HEAD"

    # The request body: buffered when middleware applies, otherwise passed through.
    if active is not None and incoming.has_body:
        buffered = await _buffer(incoming.body, ctx.max_buffer)
        if buffered.fits:
            request.body = BufferedBodyStream(buffered.join())
            request.trailers = incoming.trailers()
            flow.timings.request_complete = time.monotonic()
        else:
            _oversize(ctx, flow, active, "request", buffered.size)
            request.body = PassthroughBody(_chain(buffered.chunks, incoming.body))
    elif incoming.has_body:
        request.body = PassthroughBody(incoming.body)
    else:
        request.body = BufferedBodyStream()
        flow.timings.request_complete = time.monotonic()

    if active is not None:
        outcome = await active.run_request()
        if outcome.action is Action.DENY:
            log.info("flow %s denied by middleware %r", flow.id, outcome.by)
            await send_simple(
                responder, 403, f"denied by middleware {outcome.by}", head_only=head_only
            )
            return
        if outcome.action is Action.STOP:
            if flow.response is None:
                log.error("middleware %r stopped flow %s without a response", outcome.by, flow.id)
                await send_simple(
                    responder,
                    502,
                    f"middleware {outcome.by} stopped the request",
                    head_only=head_only,
                )
                return
            await _respond(ctx, flow, None, responder, None)
            return
        if incoming.has_body and isinstance(request.body, BufferedBodyStream):
            data = await active.run_request_chunk(await request.body.read_all(), True)
            request.body = BufferedBodyStream(data)

    body = request.body
    body_buffered = isinstance(body, BufferedBodyStream)
    # A middleware may have given a bodiless request a body by assignment.
    has_body = incoming.has_body or (isinstance(body, BufferedBodyStream) and len(body) > 0)
    mutation = active is not None and active.mutation_capable and not active.request_skipped
    request_framing, request_length = framing.plan(
        has_body=has_body,
        mutation_capable=mutation,
        buffered_length=len(body) if isinstance(body, BufferedBodyStream) else None,
        declared=incoming.declared_length,
    )

    def request_trailers() -> Headers | None:
        return request.trailers if body_buffered else incoming.trailers()

    try:
        upstream = await _send_upstream(
            ctx, flow, target, request_framing, request_length, has_body, request_trailers
        )
    except UpstreamError as e:
        status = 504 if isinstance(e, UpstreamTimeout) else 502
        log.warning("flow %s: %s", flow.id, e)
        await send_simple(responder, status, str(e), head_only=head_only)
        return
    await _respond(ctx, flow, upstream, responder, active)


async def _send_upstream(
    ctx: ExchangeContext,
    flow: Flow,
    target: Target,
    request_framing: framing.Framing,
    length: int | None,
    has_body: bool,
    trailers: Callable[[], Headers | None],
) -> UpstreamResponse:
    request = flow.request
    timings = flow.timings
    replayable = not has_body or isinstance(request.body, BufferedBodyStream)
    timings.upstream_requested = time.monotonic()
    for attempt in (1, 2):
        lease = await ctx.pool.acquire(target)
        conn = lease.connection
        timings.upstream_acquired = time.monotonic()
        timings.connection_reused = lease.reused
        flow.server_conn = ServerConn(
            host=target.host,
            port=target.port,
            protocol=conn.protocol,
            tls=target.tls,
            sni=target.sni,
            address=lease.address,
        )
        headers = framing.apply(
            request.headers, request_framing, length, h1=conn.protocol == "http/1.1"
        )
        if has_body:
            body = _timed(flow, request.body.__aiter__())
        else:
            body = empty()
            timings.request_sent = time.monotonic()
        try:
            upstream = await conn.request(
                request, headers, has_body=has_body, body=body, trailers=trailers
            )
        except UpstreamConnectError:
            retry = (
                attempt == 1
                and lease.reused
                and isinstance(conn, H1Upstream)
                and request.method in IDEMPOTENT
                and replayable
            )
            if not retry:
                raise
            log.debug("flow %s: reused connection failed; retrying on a new one", flow.id)
        else:
            timings.response_headers = time.monotonic()
            return upstream
    raise UpstreamConnectError("unreachable")  # pragma: no cover - the loop returns or raises


async def _respond(
    ctx: ExchangeContext,
    flow: Flow,
    upstream: UpstreamResponse | None,
    responder: Responder,
    active: FlowPipeline | None,
) -> None:
    """Run the response hooks (if any), frame the response, and send it to the client."""
    request = flow.request
    source = _Body(upstream.body if upstream is not None else empty())
    try:
        if upstream is not None:
            flow.response = upstream.response
        response = flow.response
        if response is None:  # pragma: no cover - callers guarantee one
            return
        allows_body = framing.response_allows_body(request.method, response.status)
        declared = upstream.declared_length if upstream is not None else None

        if upstream is not None and active is not None and not active.response_skipped:
            buffered = await _buffer(source.__aiter__(), ctx.max_buffer)
            if buffered.fits:
                response.body = BufferedBodyStream(buffered.join())
                response.trailers = upstream.trailers()
            else:
                _oversize(ctx, flow, active, "response", buffered.size)
                response.body = PassthroughBody(_chain(buffered.chunks, source.__aiter__()))
            if not active.response_skipped:
                outcome = await active.run_response()
                if outcome.action is Action.DENY:
                    log.info("flow %s response denied by middleware %r", flow.id, outcome.by)
                    await send_simple(
                        responder,
                        502,
                        f"denied by middleware {outcome.by}",
                        head_only=request.method == "HEAD",
                    )
                    return
                response = flow.response or response
                if allows_body and isinstance(response.body, BufferedBodyStream):
                    data = await active.run_response_chunk(await response.body.read_all(), True)
                    response.body = BufferedBodyStream(data)
        elif upstream is not None:
            response.body = PassthroughBody(source.__aiter__())

        body = response.body
        mutation = active is not None and active.mutation_capable and not active.response_skipped
        response_framing, length = framing.plan(
            has_body=allows_body,
            mutation_capable=mutation,
            buffered_length=len(body) if isinstance(body, BufferedBodyStream) else None,
            declared=declared,
        )
        headers = framing.apply(
            response.headers, response_framing, length, h1=responder.protocol == "http/1.1"
        )
        await responder.start(response, headers, end=not allows_body)
        if allows_body:
            async for chunk in body:
                await responder.data(chunk)
            trailers = response.trailers
            if trailers is None and upstream is not None:
                trailers = upstream.trailers()
            await responder.end(trailers)
        # Read what's left (e.g. the end of a HEAD response) so the connection can be reused.
        if upstream is not None and not source.finished:
            async for _ in source:
                pass
    finally:
        if upstream is not None and not source.finished:
            await upstream.discard()


async def _timed(flow: Flow, body: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """The request body, stamping when its last byte was handed upstream.

    Both upstream layers send the whole body before waiting for the response
    head, so the body running out is when the request was sent. A passed-through
    body is read from the client as it goes, so that is also when it was read.
    """
    async for chunk in body:
        yield chunk
    now = time.monotonic()
    flow.timings.request_sent = now
    if flow.timings.request_complete is None:
        flow.timings.request_complete = now


def _oversize(
    ctx: ExchangeContext, flow: Flow, run: FlowPipeline, direction: str, size: int
) -> None:
    if direction == "request":
        run.request_skipped = True
    run.response_skipped = True
    ctx.metrics.oversize_passthrough.inc(direction)
    log.warning(
        "flow %s: %s body is over body.max_buffer_bytes (%d+ bytes); "
        "streaming it through without middleware",
        flow.id,
        direction,
        size,
    )


async def _websocket(
    ctx: ExchangeContext,
    incoming: IncomingRequest,
    switch: Callable[[Headers], Awaitable[ByteStream]],
    responder: Responder,
) -> None:
    """Forward the upgrade on a dedicated HTTP/1.1 connection; on 101, relay raw bytes.

    No middleware runs for WebSocket flows, including the upgrade request itself.
    """
    request = incoming.request
    target, _, _ = ctx.route(request)
    flow = Flow(
        type=FlowType.WEBSOCKET, protocol=ctx.protocol, client_conn=ctx.client_conn, request=request
    )
    ctx.metrics.flows.inc(FlowType.WEBSOCKET.value)
    head_only = request.method == "HEAD"
    try:
        stream, address = await ctx.pool.open_dedicated(target)
    except UpstreamError as e:
        log.warning("flow %s: %s", flow.id, e)
        await send_simple(responder, 504 if isinstance(e, UpstreamTimeout) else 502, str(e))
        return
    flow.server_conn = ServerConn(
        target.host, target.port, "http/1.1", target.tls, target.sni, address
    )
    upstream_conn = H1Upstream(stream, read_timeout=ctx.idle_timeout)
    headers = request.headers.copy()
    headers.set("connection", "Upgrade")
    headers.set("upgrade", incoming.raw_headers.get("upgrade") or "websocket")
    try:
        upstream = await upstream_conn.request(
            request, headers, has_body=False, body=empty(), trailers=lambda: None
        )
    except UpstreamError as e:
        log.warning("flow %s: %s", flow.id, e)
        await send_simple(responder, 502, str(e), head_only=head_only)
        return
    if upstream.switched is None:
        try:
            await _respond(ctx, flow, upstream, responder, None)
        finally:
            await upstream_conn.close()
        return
    reply = upstream.response.headers.copy()
    reply.set("connection", "Upgrade")
    reply.set("upgrade", incoming.raw_headers.get("upgrade") or "websocket")
    client = await switch(reply)
    await relay(client, upstream.switched, idle_timeout=ctx.idle_timeout)
