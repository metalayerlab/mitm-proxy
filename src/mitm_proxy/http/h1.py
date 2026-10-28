"""HTTP/1.1 on h11: the client-facing server and the upstream client.

h11 is a state machine; this module owns the I/O. The server handles one
request at a time (pipelined requests wait their turn, as h11 enforces) and
reuses the connection only when both sides finished cleanly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import h11

from mitm_proxy.auth import CHALLENGE, proxy_authorized
from mitm_proxy.errors import ProtocolError, UpstreamConnectError, UpstreamTimeout
from mitm_proxy.http.body import empty
from mitm_proxy.http.flow import Request, Response, split_authority
from mitm_proxy.http.framing import declared_length, response_allows_body
from mitm_proxy.http.headers import Headers
from mitm_proxy.http.messages import IncomingRequest, Responder, UpstreamResponse
from mitm_proxy.http.normalize import (
    h1_request_headers,
    h1_response_headers,
    normalize,
    parse_target,
)
from mitm_proxy.interception.base import InterceptedConnection, TunnelHandler
from mitm_proxy.streams import ByteStream, prepend

log = logging.getLogger(__name__)

MAX_HEAD = 64 * 1024
RequestHandler = Callable[[IncomingRequest, Responder], Awaitable[None]]


@dataclass
class ProxyMode:
    """Explicit-proxy behaviour: absolute-form requests, CONNECT, and proxy authentication."""

    auth: str  # "user:password", or "" for none
    on_connect: TunnelHandler
    on_auth_failure: Callable[[], None] | None = None


class _Closed(Exception):
    """The peer went away or idled out."""


class H1Server:
    def __init__(
        self,
        stream: ByteStream,
        handler: RequestHandler,
        *,
        idle_timeout: float,
        client_addr: tuple[str, int] = ("", 0),
        default_scheme: str = "https",
        default_authority: str = "",
        proxy: ProxyMode | None = None,
    ) -> None:
        self.stream = stream
        self.handler = handler
        self.idle_timeout = idle_timeout
        self.client_addr = client_addr
        self.default_scheme = default_scheme
        self.default_authority = default_authority
        self.proxy = proxy
        self.conn = h11.Connection(h11.SERVER, max_incomplete_event_size=MAX_HEAD)
        self._handed_off = False

    async def _send(self, event: Any) -> None:
        data = self.conn.send(event)
        if data:
            await self.stream.write(data)

    async def _next_event(self) -> Any:
        while True:
            event = self.conn.next_event()
            if event is not h11.NEED_DATA:
                return event
            try:
                data = await asyncio.wait_for(self.stream.read(), self.idle_timeout)
            except TimeoutError:
                raise _Closed("idle") from None
            except (OSError, ConnectionError) as e:
                raise _Closed(str(e)) from None
            self.conn.receive_data(data)

    async def run(self) -> None:
        try:
            while True:
                try:
                    event = await self._next_event()
                except _Closed:
                    return
                except h11.RemoteProtocolError as e:
                    await self._fail(e.error_status_hint, f"bad request: {e}")
                    return
                if isinstance(event, h11.ConnectionClosed) or event is h11.PAUSED:
                    return
                if not isinstance(event, h11.Request):  # pragma: no cover - h11 guarantees it
                    return
                await self._handle(event)
                if self._handed_off or not self._next_cycle():
                    return
        except (OSError, ConnectionError, _Closed):
            pass
        finally:
            if not self._handed_off:
                await self.stream.close()

    def _next_cycle(self) -> bool:
        """Finish reading a request whose body nobody consumed, then reset for the next one."""
        try:
            while self.conn.their_state is h11.SEND_BODY:
                event = self.conn.next_event()
                if event is h11.NEED_DATA:
                    return False
            if self.conn.our_state is h11.DONE and self.conn.their_state is h11.DONE:
                self.conn.start_next_cycle()
                return True
        except h11.ProtocolError:
            pass
        return False

    async def _fail(self, status: int, message: str) -> None:
        """Answer with an error if a response can still be sent."""
        if self.conn.our_state not in (h11.IDLE, h11.SEND_RESPONSE):
            return
        body = (message + "\n").encode()
        headers = Response.make(status, body).headers
        headers.set("content-length", str(len(body)))
        headers.set("connection", "close")
        with contextlib.suppress(h11.ProtocolError, OSError, ConnectionError):
            await self._send(
                h11.Response(status_code=status, headers=headers.to_bytes(), reason=b"")
            )
            await self._send(h11.Data(data=body))
            await self._send(h11.EndOfMessage())

    async def _handle(self, event: h11.Request) -> None:
        method = event.method.decode("latin-1")
        target = event.target.decode("latin-1")
        raw = Headers.from_bytes(event.headers)

        if self.proxy is not None and not proxy_authorized(
            self.proxy.auth, raw.get("proxy-authorization")
        ):
            if self.proxy.on_auth_failure is not None:
                self.proxy.on_auth_failure()
            await self._send(
                h11.Response(
                    status_code=407,
                    headers=[
                        (b"proxy-authenticate", CHALLENGE.encode()),
                        (b"content-length", b"0"),
                    ],
                    reason=b"Proxy Authentication Required",
                )
            )
            await self._send(h11.EndOfMessage())
            return

        if method == "CONNECT":
            await self._connect(target)
            return

        try:
            scheme, authority, path = parse_target(target)
        except ProtocolError as e:
            await self._fail(400, str(e))
            return
        if self.proxy is not None:
            if scheme != "http" or not authority:
                await self._fail(400, "this is a proxy: send absolute http:// URLs, or CONNECT")
                return
        else:
            scheme = scheme or self.default_scheme
            authority = authority or raw.get("host") or self.default_authority

        upgrade = raw.get("upgrade", "") or ""
        connection = raw.get("connection", "") or ""
        websocket = "websocket" in upgrade.lower() and "upgrade" in connection.lower()
        declared = declared_length(raw)
        has_body = "transfer-encoding" in raw or (declared or 0) > 0
        te = raw.get("te", "") or ""

        trailers: list[Headers | None] = [None]
        incoming = IncomingRequest(
            request=Request(method, scheme, authority, path, normalize(raw)),
            body=self._body(trailers),
            has_body=has_body,
            declared_length=declared,
            trailers=lambda: trailers[0],
            accepts_trailers="trailers" in te.lower(),
            websocket=self._switch if websocket else None,
            raw_headers=raw,
        )
        responder = _H1Responder(self, accepts_trailers=incoming.accepts_trailers)
        try:
            await self.handler(incoming, responder)
        except (OSError, ConnectionError, _Closed):
            raise
        except ProtocolError as e:
            raise _Closed(str(e)) from None  # the client went away mid-body
        except h11.ProtocolError as e:
            log.warning("HTTP/1.1 protocol error: %s", e)
            await self._fail(400 if not responder.started else 502, "protocol error")
        except Exception:
            log.exception("request handler failed")
            if not responder.started:
                await self._fail(502, "internal proxy error")

    async def _body(self, trailers: list[Headers | None]) -> AsyncIterator[bytes]:
        if self.conn.they_are_waiting_for_100_continue:
            await self._send(h11.InformationalResponse(status_code=100, headers=[]))
        while True:
            event = await self._next_event()
            if isinstance(event, h11.Data):
                yield bytes(event.data)
            elif isinstance(event, h11.EndOfMessage):
                trailers[0] = Headers.from_bytes(event.headers) if event.headers else None
                return
            else:
                raise ProtocolError("client closed during the request body")

    async def _drain_to_pause(self) -> bytes:
        """Consume the request's end-of-message; return bytes that follow it."""
        while self.conn.their_state is h11.SEND_BODY:
            event = await self._next_event()
            if isinstance(event, h11.ConnectionClosed):
                raise _Closed("closed")
        return bytes(self.conn.trailing_data[0])

    async def _switch(self, headers: Headers) -> ByteStream:
        """Answer 101 Switching Protocols and hand the raw client stream over."""
        trailing = await self._drain_to_pause()
        await self._send(
            h11.InformationalResponse(status_code=101, headers=headers.to_bytes(), reason=b"")
        )
        self._handed_off = True
        return prepend(self.stream, trailing)

    async def _connect(self, target: str) -> None:
        if self.proxy is None:
            await self._fail(405, "CONNECT is not allowed here")
            return
        host, port = split_authority(target, 0)
        if not host or not 0 < port < 65536:
            await self._fail(400, "CONNECT needs host:port")
            return
        trailing = await self._drain_to_pause()

        async def accept() -> None:
            await self._send(
                h11.Response(status_code=200, headers=[], reason=b"Connection established")
            )

        async def reject(status: int, message: str) -> None:
            await self._fail(status, message)
            await self.stream.close()

        self._handed_off = True
        await self.proxy.on_connect(
            InterceptedConnection(
                stream=prepend(self.stream, trailing),
                target_host=host,
                target_port=port,
                client_addr=self.client_addr,
                accept=accept,
                reject=reject,
            )
        )


class _H1Responder:
    protocol = "http/1.1"

    def __init__(self, server: H1Server, *, accepts_trailers: bool) -> None:
        self.server = server
        self.accepts_trailers = accepts_trailers
        self.started = False
        self._chunked = False

    async def start(self, response: Response, headers: Headers, *, end: bool) -> None:
        self.started = True
        self._chunked = (headers.get("transfer-encoding") or "").lower() == "chunked"
        await self.server._send(
            h11.Response(
                status_code=response.status,
                headers=h1_response_headers(headers),
                reason=response.reason.encode("latin-1", "replace"),
            )
        )
        if end:
            await self.end(None)

    async def data(self, chunk: bytes) -> None:
        if chunk:
            await self.server._send(h11.Data(data=chunk))

    async def end(self, trailers: Headers | None) -> None:
        if trailers and self._chunked and self.accepts_trailers:
            await self.server._send(h11.EndOfMessage(headers=trailers.to_bytes()))
        else:
            if trailers:
                log.debug("dropping response trailers the client can't receive")
            await self.server._send(h11.EndOfMessage())


class H1Upstream:
    """One HTTP/1.1 connection to an origin, used for one request at a time."""

    protocol = "http/1.1"

    def __init__(
        self,
        stream: ByteStream,
        *,
        read_timeout: float,
        on_done: Callable[[H1Upstream, bool], Awaitable[None]] | None = None,
    ) -> None:
        self.stream = stream
        self.read_timeout = read_timeout
        self.on_done = on_done
        self.conn = h11.Connection(h11.CLIENT, max_incomplete_event_size=MAX_HEAD)
        self.busy = False
        self.requests = 0
        self.closed = False
        self.last_raw_headers = Headers()  # the last response's headers, as received

    def healthy(self) -> bool:
        return not self.closed and not self.stream.is_closing()

    def can_take_request(self) -> bool:
        return not self.busy and self.healthy() and self.conn.our_state is h11.IDLE

    @property
    def active(self) -> int:
        return 1 if self.busy else 0

    async def _send(self, event: Any) -> None:
        data = self.conn.send(event)
        if data:
            await self.stream.write(data)

    async def _next_event(self) -> Any:
        while True:
            event = self.conn.next_event()
            if event is not h11.NEED_DATA:
                return event
            try:
                data = await asyncio.wait_for(self.stream.read(), self.read_timeout)
            except TimeoutError:
                raise UpstreamTimeout("the origin did not answer in time") from None
            except (OSError, ConnectionError) as e:
                raise UpstreamConnectError(f"connection to the origin failed: {e}") from e
            self.conn.receive_data(data)

    async def request(
        self,
        request: Request,
        headers: Headers,
        *,
        has_body: bool,
        body: AsyncIterator[bytes],
        trailers: Callable[[], Headers | None],
    ) -> UpstreamResponse:
        """Send a request (headers already framed) and return the response head and body.

        Raises UpstreamError subclasses; the connection is then unusable.
        """
        self.busy = True
        self.requests += 1
        try:
            await self._send(
                h11.Request(
                    method=request.method.encode("latin-1"),
                    target=request.path.encode("latin-1"),
                    headers=h1_request_headers(request, headers),
                )
            )
            if has_body:
                async for chunk in body:
                    if chunk:
                        await self._send(h11.Data(data=chunk))
                chunked = (headers.get("transfer-encoding") or "").lower() == "chunked"
                extra = trailers() if chunked else None
                await self._send(h11.EndOfMessage(headers=extra.to_bytes() if extra else []))
            else:
                await self._send(h11.EndOfMessage())
            while True:
                event = await self._next_event()
                if isinstance(event, h11.InformationalResponse):
                    if event.status_code == 101:
                        return self._switched(event)
                    continue
                if isinstance(event, h11.Response):
                    break
                raise UpstreamConnectError("the origin closed the connection without a response")
        except (OSError, ConnectionError, h11.ProtocolError) as e:
            await self._finish(reusable=False)
            raise UpstreamConnectError(f"connection to the origin failed: {e}") from e
        except BaseException:
            await self._finish(reusable=False)
            raise
        raw = Headers.from_bytes(event.headers)
        self.last_raw_headers = raw
        response = Response(
            status=event.status_code,
            headers=normalize(raw),
            reason=event.reason.decode("latin-1"),
        )
        holder: list[Headers | None] = [None]
        return UpstreamResponse(
            response=response,
            body=self._body(holder),
            has_body=response_allows_body(request.method, event.status_code),
            declared_length=declared_length(raw),
            trailers=lambda: holder[0],
            discard=lambda: self._finish(reusable=False),
        )

    def _switched(self, event: h11.InformationalResponse) -> UpstreamResponse:
        """The origin agreed to switch protocols: hand the raw stream over."""
        self.busy = False
        self.closed = True  # no longer HTTP: never pooled again
        response = Response(
            status=101,
            headers=normalize(Headers.from_bytes(event.headers)),
            reason=event.reason.decode("latin-1"),
        )
        return UpstreamResponse(
            response=response,
            body=empty(),
            has_body=False,
            switched=prepend(self.stream, bytes(self.conn.trailing_data[0])),
        )

    async def _body(self, trailers: list[Headers | None]) -> AsyncIterator[bytes]:
        ok = False
        try:
            while True:
                event = await self._next_event()
                if isinstance(event, h11.Data):
                    yield bytes(event.data)
                elif isinstance(event, h11.EndOfMessage):
                    trailers[0] = Headers.from_bytes(event.headers) if event.headers else None
                    ok = True
                    return
                else:
                    raise UpstreamConnectError("the origin closed the connection mid-body")
        except h11.ProtocolError as e:
            raise UpstreamConnectError(f"the origin sent a malformed response: {e}") from e
        finally:
            await self._finish(reusable=ok)

    async def _finish(self, *, reusable: bool) -> None:
        if not self.busy:
            return
        self.busy = False
        if reusable and self.conn.our_state is h11.DONE and self.conn.their_state is h11.DONE:
            try:
                self.conn.start_next_cycle()
            except h11.ProtocolError:
                reusable = False
        else:
            reusable = False
        if not reusable:
            await self.close()
        if self.on_done is not None:
            await self.on_done(self, reusable)

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            await self.stream.close()
