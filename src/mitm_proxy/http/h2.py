"""HTTP/2 on h2: the client-facing server and the upstream client.

h2 is a state machine with no I/O; this module drives it. Connection-level
state (HPACK, SETTINGS, flow-control windows) lives only in the H2Connection
here. Each stream is its own flow, served by its own task.

Flow control is the backpressure: received DATA is acknowledged (window
re-opened) only when the consumer takes it, and sending waits for the peer's
window.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import h2.config
import h2.connection
import h2.errors
import h2.events
import h2.exceptions
import h2.settings

from mitm_proxy.errors import ProtocolError, UpstreamConnectError, UpstreamTimeout
from mitm_proxy.http.flow import Request, Response
from mitm_proxy.http.framing import declared_length, response_allows_body
from mitm_proxy.http.headers import Headers
from mitm_proxy.http.messages import IncomingRequest, Responder, UpstreamResponse
from mitm_proxy.http.normalize import (
    h2_request_headers,
    h2_response_headers,
    request_from_h2,
    response_from_h2,
)
from mitm_proxy.streams import ByteStream

if TYPE_CHECKING:
    # Types only: hyperframe is h2's own dependency, not one of ours.
    from h2.events import Event
    from hyperframe.frame import Frame, GoAwayFrame

log = logging.getLogger(__name__)

WINDOW = 1024 * 1024
MAX_CONCURRENT_STREAMS = 100
RequestHandler = Callable[[IncomingRequest, Responder], Awaitable[None]]
_SC = h2.settings.SettingCodes


@dataclass
class _Stream:
    queue: asyncio.Queue[tuple[bytes, int] | BaseException | None] = field(
        default_factory=asyncio.Queue
    )
    trailers: Headers | None = None
    head: asyncio.Future[list[tuple[bytes, bytes]]] | None = None
    done: bool = False


class _ServerConnection(h2.connection.H2Connection):
    """h2's connection, but open streams may finish after a client's graceful GOAWAY.

    RFC 9113 (section 6.8) lets streams up to a GOAWAY's last_stream_id complete. h2
    instead moves the whole connection to CLOSED on any GOAWAY it receives, then refuses
    to send DATA (and drops what's queued), so a download in progress would be cut off.
    For a graceful (NO_ERROR) GOAWAY this keeps the connection open: the client opens no
    new streams, and the server stops once the open ones are done. This overrides a
    private h2 method (4.x); the GOAWAY regression test pins it.
    """

    def _receive_goaway_frame(self, frame: GoAwayFrame) -> tuple[list[Frame], list[Event]]:
        open_state = h2.connection.ConnectionState.SERVER_OPEN
        if frame.error_code != 0 or self.state_machine.state is not open_state:
            return super()._receive_goaway_frame(frame)
        event = h2.events.ConnectionTerminated()
        event.error_code = h2.errors.ErrorCodes.NO_ERROR
        event.last_stream_id = frame.last_stream_id
        event.additional_data = frame.additional_data or None
        return [], [event]


def _regular(raw: list[tuple[bytes, bytes]]) -> Headers:
    return Headers.from_bytes((k, v) for k, v in raw if not k.startswith(b":"))


class _H2Connection:
    def __init__(self, stream: ByteStream, *, client_side: bool) -> None:
        self.stream = stream
        connection = h2.connection.H2Connection if client_side else _ServerConnection
        self.conn = connection(
            h2.config.H2Configuration(client_side=client_side, header_encoding=None)
        )
        self.streams: dict[int, _Stream] = {}
        self.closed = False
        self._window = asyncio.Condition()

    async def flush(self) -> None:
        data = self.conn.data_to_send()
        if data:
            await self.stream.write(data)

    def _initiate(self, settings: dict[int, int]) -> None:
        self.conn.initiate_connection()
        self.conn.update_settings(settings)
        self.conn.increment_flow_control_window(WINDOW - 65535)

    async def _notify_window(self) -> None:
        async with self._window:
            self._window.notify_all()

    async def send_body(self, stream_id: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            async with self._window:
                while True:
                    if self.closed:
                        raise ConnectionResetError("HTTP/2 connection closed")
                    try:
                        window = self.conn.local_flow_control_window(stream_id)
                    except h2.exceptions.StreamClosedError:
                        raise ConnectionResetError("HTTP/2 stream closed") from None
                    if window > 0:
                        break
                    await self._window.wait()
            size = min(window, self.conn.max_outbound_frame_size, len(view))
            try:
                self.conn.send_data(stream_id, bytes(view[:size]))
            except (h2.exceptions.StreamClosedError, h2.exceptions.ProtocolError):
                raise ConnectionResetError("HTTP/2 stream closed") from None
            view = view[size:]
            await self.flush()

    async def body(
        self, stream_id: int, state: _Stream, timeout: float | None = None
    ) -> AsyncIterator[bytes]:
        """The stream's DATA, acknowledged as it is consumed."""
        while True:
            try:
                item = await asyncio.wait_for(state.queue.get(), timeout)
            except TimeoutError:
                raise UpstreamTimeout("the origin stopped sending") from None
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            data, flow_length = item
            if flow_length:
                with contextlib.suppress(h2.exceptions.ProtocolError):
                    self.conn.acknowledge_received_data(flow_length, stream_id)
                await self.flush()
            if data:
                yield data

    def _release(self, sid: int) -> _Stream | None:
        """Forget a finished stream, giving back the window of DATA nobody will read.

        Received DATA is acknowledged only when it's consumed, so DATA still queued
        when a stream ends early (an error before the body was read, a reset, a
        discarded response) would otherwise be lost from the connection window for
        good, and every other stream on the connection would stall.
        """
        state = self.streams.pop(sid, None)
        if state is None:
            return None
        state.done = True  # DATA arriving from now on is acknowledged in _on_data
        unread = 0
        while not state.queue.empty():
            item = state.queue.get_nowait()
            if isinstance(item, tuple):
                unread += item[1]
        if unread:
            # A closed stream still updates the connection window (h2 handles it).
            with contextlib.suppress(h2.exceptions.ProtocolError):
                self.conn.acknowledge_received_data(unread, sid)
        return state

    def _on_data(self, event: h2.events.DataReceived) -> None:
        sid = event.stream_id
        state = self.streams.get(sid) if sid is not None else None
        length = event.flow_controlled_length or 0
        if state is None or state.done:
            # Nobody will read it: re-open the window so other streams aren't starved.
            if sid is not None and length:
                with contextlib.suppress(h2.exceptions.ProtocolError):
                    self.conn.acknowledge_received_data(length, sid)
            return
        state.queue.put_nowait((event.data or b"", length))

    def _send_headers(self, stream_id: int, headers: list[tuple[bytes, bytes]], end: bool) -> None:
        try:
            self.conn.send_headers(stream_id, headers, end_stream=end)
        except (h2.exceptions.StreamClosedError, h2.exceptions.ProtocolError) as e:
            raise ConnectionResetError(f"HTTP/2 stream closed: {e}") from None

    def _end(self, stream_id: int, trailers: Headers | None) -> None:
        try:
            if trailers:
                self.conn.send_headers(stream_id, trailers.to_bytes(), end_stream=True)
            else:
                self.conn.end_stream(stream_id)
        except (h2.exceptions.StreamClosedError, h2.exceptions.ProtocolError) as e:
            raise ConnectionResetError(f"HTTP/2 stream closed: {e}") from None

    def reset(self, stream_id: int, code: h2.errors.ErrorCodes) -> None:
        with contextlib.suppress(h2.exceptions.ProtocolError):
            self.conn.reset_stream(stream_id, error_code=code)


class H2Server(_H2Connection):
    def __init__(self, stream: ByteStream, handler: RequestHandler, *, idle_timeout: float) -> None:
        super().__init__(stream, client_side=False)
        self.handler = handler
        self.idle_timeout = idle_timeout
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._goaway = False  # the client said it will open no more streams
        self._drained = asyncio.Event()  # set while no stream is being served
        self._drained.set()

    async def run(self) -> None:
        self._initiate(
            {_SC.MAX_CONCURRENT_STREAMS: MAX_CONCURRENT_STREAMS, _SC.INITIAL_WINDOW_SIZE: WINDOW}
        )
        try:
            await self.flush()
            while not (self._goaway and not self._tasks):
                if self._goaway:
                    # The client opens no new streams, but the open ones still need its
                    # WINDOW_UPDATEs and request DATA: read until they've finished.
                    data = await self._read_until_drained()
                else:
                    timeout = None if self._tasks else self.idle_timeout
                    try:
                        data = await asyncio.wait_for(self.stream.read(), timeout)
                    except TimeoutError:
                        self.conn.close_connection()
                        await self.flush()
                        break
                if not data:
                    break
                try:
                    events = self.conn.receive_data(data)
                except h2.exceptions.ProtocolError as e:
                    log.warning("HTTP/2 protocol error from the client: %s", e)
                    self.conn.close_connection(error_code=h2.errors.ErrorCodes.PROTOCOL_ERROR)
                    await self.flush()
                    break
                for event in events:
                    await self._dispatch(event)
                await self.flush()
            if self._tasks:
                await asyncio.wait(list(self._tasks.values()), timeout=self.idle_timeout)
        except (OSError, ConnectionError):
            pass
        finally:
            self.closed = True
            await self._notify_window()
            streams = list(self._tasks.values())
            for task in streams:
                task.cancel()
            await self.stream.close()
            # Let cancelled streams finish their cleanup: on_done runs there (0006), each
            # bounded by the hook timeout.
            await asyncio.gather(*streams, return_exceptions=True)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        for sid, known in list(self._tasks.items()):
            if known is task:
                del self._tasks[sid]
        if not self._tasks:
            self._drained.set()

    async def _read_until_drained(self) -> bytes:
        """The next bytes from the client, or b"" once no stream is left (or on idle)."""
        read = asyncio.ensure_future(self.stream.read())
        drained = asyncio.ensure_future(self._drained.wait())
        done, _ = await asyncio.wait(
            {read, drained}, timeout=self.idle_timeout, return_when=asyncio.FIRST_COMPLETED
        )
        drained.cancel()
        if read in done:
            return read.result()
        read.cancel()  # the connection is ending: nothing more will be read from it
        await asyncio.gather(read, return_exceptions=True)
        return b""

    async def _dispatch(self, event: h2.events.Event) -> None:
        if isinstance(event, h2.events.RequestReceived) and event.stream_id is not None:
            sid = event.stream_id
            state = _Stream()
            self.streams[sid] = state
            task = asyncio.create_task(
                self._serve(sid, list(event.headers or []), not event.stream_ended, state)
            )
            self._tasks[sid] = task
            self._drained.clear()
            task.add_done_callback(self._task_done)
        elif isinstance(event, h2.events.DataReceived):
            self._on_data(event)
        elif isinstance(event, h2.events.TrailersReceived) and event.stream_id in self.streams:
            self.streams[event.stream_id].trailers = _regular(list(event.headers or []))
        elif isinstance(event, h2.events.StreamEnded) and event.stream_id in self.streams:
            self.streams[event.stream_id].queue.put_nowait(None)
        elif isinstance(event, h2.events.StreamReset) and event.stream_id is not None:
            reset = self._release(event.stream_id)
            if reset is not None:
                reset.queue.put_nowait(ConnectionResetError("the client reset the stream"))
            serving = self._tasks.get(event.stream_id)
            if serving is not None:
                serving.cancel()
            await self._notify_window()
        elif isinstance(event, h2.events.WindowUpdated | h2.events.RemoteSettingsChanged):
            await self._notify_window()
        elif isinstance(event, h2.events.ConnectionTerminated):
            self._goaway = True

    async def _serve(
        self, sid: int, raw: list[tuple[bytes, bytes]], has_body: bool, state: _Stream
    ) -> None:
        responder = _H2Responder(self, sid)
        try:
            try:
                request = request_from_h2(raw)
            except ProtocolError:
                self.reset(sid, h2.errors.ErrorCodes.PROTOCOL_ERROR)
                return
            regular = _regular(raw)
            if request.method == "CONNECT":
                message = b"CONNECT over HTTP/2 is not supported\n"
                response = Response.make(501, message)
                response.headers.set("content-length", str(len(message)))
                await responder.start(response, response.headers, end=False)
                await responder.data(message)
                await responder.end(None)
                return
            te = regular.get("te", "") or ""
            incoming = IncomingRequest(
                request=request,
                body=self.body(sid, state),
                has_body=has_body,
                declared_length=declared_length(regular),
                trailers=lambda: state.trailers,
                accepts_trailers="trailers" in te.lower(),
                raw_headers=regular,
            )
            await self.handler(incoming, responder)
        except asyncio.CancelledError:
            raise
        except (OSError, ConnectionError, ProtocolError):
            self.reset(sid, h2.errors.ErrorCodes.CANCEL)
        except Exception:
            log.exception("request handler failed (HTTP/2 stream %d)", sid)
            self.reset(sid, h2.errors.ErrorCodes.INTERNAL_ERROR)
        finally:
            state.done = True
            self._release(sid)
            with contextlib.suppress(OSError, ConnectionError):
                await self.flush()


class _H2Responder:
    protocol = "h2"

    def __init__(self, server: H2Server, stream_id: int) -> None:
        self.server = server
        self.stream_id = stream_id
        self.started = False

    async def start(self, response: Response, headers: Headers, *, end: bool) -> None:
        self.started = True
        self.server._send_headers(self.stream_id, h2_response_headers(response, headers), end)
        await self.server.flush()

    async def data(self, chunk: bytes) -> None:
        if chunk:
            await self.server.send_body(self.stream_id, chunk)

    async def end(self, trailers: Headers | None) -> None:
        self.server._end(self.stream_id, trailers)
        await self.server.flush()


class H2Upstream(_H2Connection):
    """One HTTP/2 connection to an origin, shared by concurrent flows."""

    protocol = "h2"

    def __init__(
        self,
        stream: ByteStream,
        *,
        read_timeout: float,
        on_capacity: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(stream, client_side=True)
        self.read_timeout = read_timeout
        self.on_capacity = on_capacity
        self.goaway = False
        self.requests = 0
        self._reader: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._initiate({_SC.ENABLE_PUSH: 0, _SC.INITIAL_WINDOW_SIZE: WINDOW})
        await self.flush()
        self._reader = asyncio.create_task(self._read_loop())

    def healthy(self) -> bool:
        return not self.closed and not self.goaway and not self.stream.is_closing()

    def can_take_request(self) -> bool:
        limit = self.conn.remote_settings.max_concurrent_streams
        return self.healthy() and len(self.streams) < limit

    @property
    def active(self) -> int:
        return len(self.streams)

    async def request(
        self,
        request: Request,
        headers: Headers,
        *,
        has_body: bool,
        body: AsyncIterator[bytes],
        trailers: Callable[[], Headers | None],
    ) -> UpstreamResponse:
        if not self.healthy():
            raise UpstreamConnectError("the HTTP/2 connection to the origin is closing")
        self.requests += 1
        sid = self.conn.get_next_available_stream_id()
        head: asyncio.Future[list[tuple[bytes, bytes]]] = asyncio.get_running_loop().create_future()
        state = _Stream(head=head)
        self.streams[sid] = state
        try:
            self._send_headers(sid, h2_request_headers(request, headers), not has_body)
            await self.flush()
            if has_body:
                async for chunk in body:
                    await self.send_body(sid, chunk)
                self._end(sid, trailers())
                await self.flush()
            raw = await asyncio.wait_for(asyncio.shield(head), self.read_timeout)
        except TimeoutError:
            await self._drop(sid, cancel=True)
            raise UpstreamTimeout("the origin did not answer in time") from None
        except (OSError, ConnectionError) as e:
            await self._drop(sid, cancel=True)
            raise UpstreamConnectError(f"HTTP/2 request to the origin failed: {e}") from e
        except BaseException:
            await self._drop(sid, cancel=True)
            raise
        try:
            response = response_from_h2(raw)
        except ProtocolError as e:
            await self._drop(sid, cancel=True)
            raise UpstreamConnectError(f"the origin sent a malformed response: {e}") from e
        return UpstreamResponse(
            response=response,
            body=self._response_body(sid, state),
            has_body=response_allows_body(request.method, response.status),
            declared_length=declared_length(_regular(raw)),
            trailers=lambda: state.trailers,
            discard=lambda: self._drop(sid, cancel=True),
        )

    async def _response_body(self, sid: int, state: _Stream) -> AsyncIterator[bytes]:
        try:
            async for chunk in self.body(sid, state, self.read_timeout):
                yield chunk
        except ConnectionResetError as e:
            raise UpstreamConnectError(f"the origin reset the stream: {e}") from e
        finally:
            await self._drop(sid, cancel=not state.done)

    async def _drop(self, sid: int, *, cancel: bool) -> None:
        state = self._release(sid)
        if state is None:
            return
        if cancel:
            self.reset(sid, h2.errors.ErrorCodes.CANCEL)
        with contextlib.suppress(OSError, ConnectionError):
            await self.flush()  # the reset, and the window given back by _release
        if self.goaway and not self.streams:
            await self.close()
        if self.on_capacity is not None:
            await self.on_capacity()

    async def _read_loop(self) -> None:
        try:
            while not self.closed:
                data = await self.stream.read()
                if not data:
                    break
                try:
                    events = self.conn.receive_data(data)
                except h2.exceptions.ProtocolError as e:
                    log.warning("HTTP/2 protocol error from the origin: %s", e)
                    break
                for event in events:
                    await self._dispatch(event)
                await self.flush()
        except (OSError, ConnectionError):
            pass
        finally:
            self.closed = True
            error = UpstreamConnectError("the HTTP/2 connection to the origin closed")
            for state in self.streams.values():
                self._fail(state, error)
            await self._notify_window()
            with contextlib.suppress(OSError, ConnectionError):
                await self.stream.close()
            if self.on_capacity is not None:
                await self.on_capacity()

    @staticmethod
    def _fail(state: _Stream, error: BaseException) -> None:
        if state.head is not None and not state.head.done():
            state.head.set_exception(error)
        state.queue.put_nowait(error)

    async def _dispatch(self, event: Any) -> None:
        sid = getattr(event, "stream_id", None)
        state = self.streams.get(sid) if sid is not None else None
        if isinstance(event, h2.events.ResponseReceived) and state is not None:
            if state.head is not None and not state.head.done():
                state.head.set_result(list(event.headers or []))
        elif isinstance(event, h2.events.DataReceived):
            self._on_data(event)
        elif isinstance(event, h2.events.TrailersReceived) and state is not None:
            state.trailers = _regular(list(event.headers or []))
        elif isinstance(event, h2.events.StreamEnded) and state is not None:
            state.done = True
            state.queue.put_nowait(None)
        elif isinstance(event, h2.events.StreamReset) and state is not None:
            self._fail(state, ConnectionResetError(f"stream reset ({event.error_code!r})"))
        elif isinstance(event, h2.events.WindowUpdated | h2.events.RemoteSettingsChanged):
            await self._notify_window()
            if self.on_capacity is not None:
                await self.on_capacity()
        elif isinstance(event, h2.events.ConnectionTerminated):
            self.goaway = True
            last = event.last_stream_id or 0
            for stream_id, st in list(self.streams.items()):
                if stream_id > last:
                    self._fail(st, UpstreamConnectError("the origin refused the stream (GOAWAY)"))

    async def close(self) -> None:
        if self.closed and self._reader is None:
            return
        self.closed = True
        with contextlib.suppress(h2.exceptions.ProtocolError):
            self.conn.close_connection()
        with contextlib.suppress(OSError, ConnectionError):
            await self.flush()
        reader, self._reader = self._reader, None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        await self.stream.close()
