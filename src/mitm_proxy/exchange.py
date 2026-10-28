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
from mitm_proxy.http.flow import (
    ClientConn,
    Flow,
    FlowType,
    Outcome,
    Request,
    Response,
    ServerConn,
)
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
flows_log = logging.getLogger("mitm_proxy.flows")  # proxy.access_log (0005)
relayed_log = logging.getLogger("mitm_proxy.relayed")  # proxy.access_log_relayed (0005)

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
    server_timing: bool = False  # add a Server-Timing field to intercepted responses (0004)
    access_log: bool = False  # a line per intercepted flow (0005)
    access_log_relayed: bool = False  # a line per relayed plain-HTTP request (0005)

    def route(self, request: Request) -> tuple[Target, Pipeline | None, str]:
        if self.target is not None:
            return self.target, self.pipeline, FlowType.HTTP.value
        intercepted = self.intercepts(request)
        target = Target(request.host, request.port, tls=False)
        return target, self.pipeline if intercepted else None, "plain_http"

    def intercepts(self, request: Request) -> bool:
        """A TLS-intercepted flow, or plain HTTP to a listed host."""
        if self.target is not None:
            return True
        return self.scope is not None and self.scope.match(request.host) is not None


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


class _Recorder:
    """The client's Responder, recording what was sent: status, body bytes, and the end."""

    def __init__(self, inner: Responder) -> None:
        self.inner = inner
        self.protocol = inner.protocol
        self.status: int | None = None
        self.sent = 0
        self.finished = False

    @property
    def started(self) -> bool:
        return self.inner.started

    @started.setter
    def started(self, value: bool) -> None:
        self.inner.started = value

    async def start(self, response: Response, headers: Headers, *, end: bool) -> None:
        await self.inner.start(response, headers, end=end)
        self.status = response.status
        self.finished = end

    async def data(self, chunk: bytes) -> None:
        await self.inner.data(chunk)
        self.sent += len(chunk)

    async def end(self, trailers: Headers | None) -> None:
        await self.inner.end(trailers)
        self.finished = True


class _Tally:
    """What the access log needs: the path the exchange took, and what crossed the client leg."""

    def __init__(self, responder: Responder) -> None:
        self.responder = _Recorder(responder)
        self.received = 0  # request body bytes read from the client
        self.kind = "origin"
        self.by: str | None = None
        self.relayed: tuple[int, int] | None = None  # a WebSocket's bytes each way

    def mark(self, kind: str, by: str | None = None) -> None:
        self.kind, self.by = kind, by

    async def count(self, body: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        async for chunk in body:
            self.received += len(chunk)
            yield chunk

    def outcome(self) -> Outcome:
        r = self.responder
        if self.relayed is not None:
            return Outcome("websocket", None, 101)
        if not r.finished:
            return Outcome("aborted", None, r.status)
        return Outcome(self.kind, self.by, r.status)

    def sizes(self) -> tuple[int, int]:
        return self.relayed if self.relayed is not None else (self.received, self.responder.sent)


def _ms(seconds: float) -> str:
    return f"{seconds * 1000:.1f}"


def _quoted(text: str) -> str:
    """A quoted-string (RFC 9110) of the printable ASCII in text."""
    safe = "".join(c for c in text if " " <= c <= "~")
    return '"' + safe.replace("\\", "\\\\").replace('"', '\\"') + '"'


def server_timing(flow: Flow, local: str | None = None) -> str:
    """The proxy's Server-Timing field for flow, as of now (the response head going out).

    local names the middleware that answered or denied the request. Metric names
    carry a mitm- prefix so they can't collide with an origin's own.
    """
    t = flow.timings
    parts: list[str] = []
    if t.connect is not None:
        reused = "reused" if t.connection_reused else "new"
        parts.append(f'mitm-connect;dur={_ms(t.connect)};desc="{reused}"')
    if t.ttfb is not None:
        parts.append(f"mitm-origin;dur={_ms(t.ttfb)}")
    if t.middleware:
        parts.append(f"mitm-mw;dur={_ms(t.middleware_total)}")
    if local is not None:
        parts.append(f"mitm-local;desc={_quoted(local)}")
    parts.append(f"mitm-proxy;dur={_ms(time.monotonic() - t.started)}")
    return ", ".join(parts)


async def send_simple(
    responder: Responder,
    status: int,
    message: str,
    *,
    head_only: bool = False,
    timing: str | None = None,
) -> None:
    """A short plain-text response from the proxy itself; timing: a Server-Timing value."""
    body = (message + "\n").encode()
    response = Response.make(status, body)
    response.headers.set("content-length", str(len(body)))
    if timing is not None:
        response.headers.add("server-timing", timing)
    await responder.start(response, response.headers, end=head_only)
    if not head_only:
        await responder.data(body)
        await responder.end(None)


async def handle(ctx: ExchangeContext, incoming: IncomingRequest, responder: Responder) -> None:
    """Serve one request. Errors become proxy responses; nothing propagates but I/O errors."""
    request = incoming.request
    target, pipeline, kind = ctx.route(request)
    switch = incoming.websocket
    flow = Flow(
        type=FlowType.HTTP if switch is None else FlowType.WEBSOCKET,
        protocol=ctx.protocol,
        client_conn=ctx.client_conn,
        request=request,
    )
    ctx.metrics.flows.inc(kind if switch is None else FlowType.WEBSOCKET.value)
    tally = _Tally(responder)
    incoming.body = tally.count(incoming.body)
    run: FlowPipeline | None = None
    try:
        if switch is not None:
            await _websocket(ctx, flow, incoming, switch, tally, target)
        else:
            if pipeline is not None:
                run = pipeline.start(flow)
                run.on_failure = lambda name: ctx.metrics.middleware_failures.inc(name)
            timed = ctx.server_timing and ctx.intercepts(request)
            await _exchange(ctx, flow, incoming, tally, target, run, timed)
    finally:
        flow.timings.complete = time.monotonic()
        outcome = flow._outcome = tally.outcome()
        _access_log(ctx, flow, tally, outcome)
        # After the log line, so the log never waits on middleware. On cancellation this
        # still runs (each hook bounded by the hook timeout); the cancellation then resumes.
        try:
            if run is not None and run.applies:
                await run.run_done()
        finally:
            # After on_done, so each middleware's time includes it; WebSockets are long-lived
            # by design and relayed flows have no stages, so neither is observed (0007).
            if switch is None and ctx.intercepts(request):
                ctx.metrics.observe_flow(flow)


def _access_log(ctx: ExchangeContext, flow: Flow, tally: _Tally, outcome: Outcome) -> None:
    """The flow's access-log line (0005). Synchronous: written even when the flow is cancelled."""
    intercepted = ctx.intercepts(flow.request)
    if not (ctx.access_log if intercepted else ctx.access_log_relayed):
        return
    request, t = flow.request, flow.timings
    received, sent = tally.sizes()
    status = outcome.status if outcome.status is not None else "-"
    url = f"{request.scheme}://{request.authority}{request.path}"
    if not intercepted:
        address = f"{flow.client_conn.address[0]}:{flow.client_conn.address[1]}"
        relayed_log.info(
            "http %s %s %s %s %s req=%d resp=%d total=%s",
            flow.id,
            address,
            request.method,
            url,
            status,
            received,
            sent,
            _log_ms(t.total),
        )
        return
    connect = _log_ms(t.connect)
    if t.connect is not None and t.connection_reused:
        connect += "(reused)"
    flows_log.info(
        "%s %s>%s %s %s %s req=%d resp=%d total=%s connect=%s ttfb=%s mw=%s outcome=%s",
        flow.id,
        flow.protocol,
        flow.server_conn.protocol if flow.server_conn else "-",
        request.method,
        url,
        status,
        received,
        sent,
        _log_ms(t.total),
        connect,
        _log_ms(t.ttfb),
        _log_ms(t.middleware_total),
        outcome,
    )


def _log_ms(seconds: float | None) -> str:
    return "-" if seconds is None else f"{seconds * 1000:.1f}ms"


async def _exchange(
    ctx: ExchangeContext,
    flow: Flow,
    incoming: IncomingRequest,
    tally: _Tally,
    target: Target,
    run: FlowPipeline | None,
    timed: bool,
) -> None:
    request = flow.request
    responder = tally.responder

    def timing(local: str | None = None) -> str | None:
        return server_timing(flow, local) if timed else None

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
            tally.mark("denied", outcome.by)
            await send_simple(
                responder,
                403,
                f"denied by middleware {outcome.by}",
                head_only=head_only,
                timing=timing(outcome.by),
            )
            return
        if outcome.action is Action.STOP:
            if flow.response is None:
                log.error("middleware %r stopped flow %s without a response", outcome.by, flow.id)
                tally.mark("error", outcome.by)
                await send_simple(
                    responder,
                    502,
                    f"middleware {outcome.by} stopped the request",
                    head_only=head_only,
                    timing=timing(outcome.by),
                )
                return
            tally.mark("local", outcome.by)
            await _respond(ctx, flow, None, tally, None, timed=timed, local=outcome.by)
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
        tally.mark("error")
        await send_simple(responder, status, str(e), head_only=head_only, timing=timing())
        return
    await _respond(ctx, flow, upstream, tally, active, timed=timed)


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
    tally: _Tally,
    active: FlowPipeline | None,
    *,
    timed: bool = False,
    local: str | None = None,
) -> None:
    """Run the response hooks (if any), frame the response, and send it to the client.

    timed: add the proxy's Server-Timing field; local: the middleware that answered.
    """
    request = flow.request
    responder = tally.responder
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
                    tally.mark("denied", outcome.by)
                    await send_simple(
                        responder,
                        502,
                        f"denied by middleware {outcome.by}",
                        head_only=request.method == "HEAD",
                        timing=server_timing(flow, outcome.by) if timed else None,
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
        if timed:
            # After the hooks, like the framing headers: middleware never sees this field.
            headers.add("server-timing", server_timing(flow, local))
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
    flow: Flow,
    incoming: IncomingRequest,
    switch: Callable[[Headers], Awaitable[ByteStream]],
    tally: _Tally,
    target: Target,
) -> None:
    """Forward the upgrade on a dedicated HTTP/1.1 connection; on 101, relay raw bytes.

    No middleware runs for WebSocket flows, including the upgrade request itself.
    """
    request = incoming.request
    responder = tally.responder
    head_only = request.method == "HEAD"
    try:
        stream, address = await ctx.pool.open_dedicated(target)
    except UpstreamError as e:
        log.warning("flow %s: %s", flow.id, e)
        tally.mark("error")
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
        tally.mark("error")
        await send_simple(responder, 502, str(e), head_only=head_only)
        return
    if upstream.switched is None:
        try:
            await _respond(ctx, flow, upstream, tally, None)
        finally:
            await upstream_conn.close()
        return
    reply = upstream.response.headers.copy()
    reply.set("connection", "Upgrade")
    reply.set("upgrade", incoming.raw_headers.get("upgrade") or "websocket")
    client = await switch(reply)
    tally.relayed = (0, 0)
    tally.relayed = await relay(client, upstream.switched, idle_timeout=ctx.idle_timeout)
