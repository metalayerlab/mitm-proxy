"""An in-memory proxy test bench: test client -> proxy server side -> pool -> fake origin.

Everything runs over MemoryStream pairs on one event loop: no sockets, no network.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

from mitm_proxy.errors import UpstreamError
from mitm_proxy.exchange import ExchangeContext, handle
from mitm_proxy.http.body import empty
from mitm_proxy.http.flow import ClientConn, Request, Response
from mitm_proxy.http.h1 import H1Server, H1Upstream, ProxyMode
from mitm_proxy.http.h2 import H2Server, H2Upstream
from mitm_proxy.http.headers import Headers
from mitm_proxy.http.messages import IncomingRequest, Responder
from mitm_proxy.metrics import Metrics
from mitm_proxy.pipeline import Middleware, Pipeline
from mitm_proxy.scope import Allowlist
from mitm_proxy.upstream.pool import Connected, ConnectionPool, Target
from tests.helpers import MemoryStream, memory_pair


@dataclass
class Seen:
    """What the origin received."""

    method: str
    authority: str
    path: str
    headers: Headers
    body: bytes
    trailers: Headers | None
    raw_headers: Headers


@dataclass
class Origin:
    """A fake origin server. Answers every request with the configured response."""

    protocol: str = "http/1.1"  # what ALPN "negotiates" for it
    status: int = 200
    headers: list[tuple[str, str]] = field(default_factory=lambda: [("content-type", "text/plain")])
    body: bytes = b"hello from origin"
    trailers: Headers | None = None
    chunked: bool = False  # h1: send the body chunked instead of with content-length
    echo: bool = False  # respond with the request body
    seen: list[Seen] = field(default_factory=list)
    connections: int = 0
    fail_connect: UpstreamError | None = None
    websocket: bool = False  # answer upgrades with 101 and echo raw bytes
    tasks: list[asyncio.Task[None]] = field(default_factory=list)

    async def handler(self, incoming: IncomingRequest, responder: Responder) -> None:
        data = b"".join([chunk async for chunk in incoming.body])
        request = incoming.request
        self.seen.append(
            Seen(
                request.method,
                request.authority,
                request.path,
                request.headers,
                data,
                incoming.trailers(),
                incoming.raw_headers,
            )
        )
        if incoming.websocket is not None and self.websocket:
            reply = Headers([("connection", "Upgrade"), ("upgrade", "websocket")])
            stream = await incoming.websocket(reply)
            while chunk := await stream.read():
                await stream.write(chunk.upper())
            await stream.close()
            return
        body = data if self.echo else self.body
        headers = Headers(self.headers)
        if not self.chunked:
            headers.set("content-length", str(len(body)))
        elif responder.protocol == "http/1.1":
            headers.set("transfer-encoding", "chunked")
        no_body = request.method == "HEAD" or self.status in (204, 304)
        await responder.start(Response(self.status, headers), headers, end=no_body)
        if not no_body:
            half = len(body) // 2
            await responder.data(body[:half])
            await responder.data(body[half:])
            await responder.end(self.trailers)

    async def serve(self, stream: MemoryStream) -> None:
        self.connections += 1
        if self.protocol == "h2":
            await H2Server(stream, self.handler, idle_timeout=5).run()
        else:
            await H1Server(stream, self.handler, idle_timeout=5).run()

    async def connector(self, target: Target, alpn: tuple[str, ...], timeout: float) -> Connected:
        if self.fail_connect is not None:
            raise self.fail_connect
        proxy_end, origin_end = memory_pair()
        self.tasks.append(asyncio.create_task(self.serve(origin_end)))
        negotiated = self.protocol if self.protocol in alpn else "http/1.1"
        if negotiated != self.protocol:
            self.protocol = negotiated
        return Connected(proxy_end, negotiated, ("192.0.2.80", target.port))


DEFAULT_TARGET = Target("example.org", 443, tls=True, sni="example.org")


@dataclass
class Bench:
    origin: Origin
    pool: ConnectionPool
    metrics: Metrics
    tasks: list[asyncio.Task[None]] = field(default_factory=list)

    def context(
        self,
        protocol: str,
        middleware: list[tuple[str, Middleware]] | None = None,
        *,
        max_buffer: int = 1 << 20,
        target: Target | None = DEFAULT_TARGET,
        scope: Allowlist | None = None,
    ) -> ExchangeContext:
        return ExchangeContext(
            pool=self.pool,
            metrics=self.metrics,
            client_conn=ClientConn(("192.0.2.1", 50000), sni="example.org"),
            protocol=protocol,
            max_buffer=max_buffer,
            idle_timeout=5,
            pipeline=Pipeline(middleware or [], hook_timeout=1),
            target=target,
            scope=scope,
        )

    def proxy(self, ctx: ExchangeContext, proxy_mode: ProxyMode | None = None) -> MemoryStream:
        """Start the proxy's client-facing server; return the client's end."""
        client_end, proxy_end = memory_pair()

        async def handler(incoming: IncomingRequest, responder: Responder) -> None:
            await handle(ctx, incoming, responder)

        server: H1Server | H2Server
        if ctx.protocol == "h2":
            server = H2Server(proxy_end, handler, idle_timeout=5)
        else:
            server = H1Server(
                proxy_end,
                handler,
                idle_timeout=5,
                default_authority="example.org",
                proxy=proxy_mode,
            )
        self.tasks.append(asyncio.create_task(server.run()))
        return client_end

    async def close(self) -> None:
        await self.pool.close()
        for task in [*self.tasks, *self.origin.tasks]:
            task.cancel()
        await asyncio.gather(*self.tasks, *self.origin.tasks, return_exceptions=True)


def bench(
    origin: Origin | None = None,
    *,
    max_per_host: int = 6,
    idle_timeout: float = 60.0,
    connect_timeout: float = 2.0,
) -> Bench:
    origin = origin or Origin()
    pool = ConnectionPool(
        origin.connector,
        max_per_host=max_per_host,
        idle_timeout=idle_timeout,
        connect_timeout=connect_timeout,
        read_timeout=5,
    )
    return Bench(origin, pool, Metrics("test"))


@dataclass
class Reply:
    status: int
    headers: Headers
    body: bytes
    trailers: Headers | None


async def _chunks(data: bytes) -> AsyncIterator[bytes]:
    if data:
        yield data


async def send(
    client: H1Upstream | H2Upstream,
    method: str = "GET",
    path: str = "/",
    *,
    body: bytes | None = None,
    headers: list[tuple[str, str]] | None = None,
    authority: str = "example.org",
    trailers: Headers | None = None,
) -> Reply:
    """Send one request through the proxy as a client would."""
    request_headers = Headers(headers or [])
    if (
        body is not None
        and client.protocol == "http/1.1"
        and "transfer-encoding" not in request_headers
    ):
        request_headers.set("content-length", str(len(body)))
    request = Request(method, "https", authority, path, request_headers)
    response = await client.request(
        request,
        request_headers,
        has_body=body is not None,
        body=_chunks(body or b"") if body is not None else empty(),
        trailers=lambda: trailers,
    )
    data = b"".join([chunk async for chunk in response.body])
    return Reply(response.response.status, response.response.headers, data, response.trailers())


async def client_for(stream: MemoryStream, protocol: str) -> H1Upstream | H2Upstream:
    if protocol == "h2":
        client = H2Upstream(stream, read_timeout=5)
        await client.start()
        return client
    return H1Upstream(stream, read_timeout=5)


Handler = Callable[[IncomingRequest, Responder], Awaitable[None]]
