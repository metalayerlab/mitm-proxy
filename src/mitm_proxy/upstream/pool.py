"""The upstream connection pool.

Keyed by (host, port, TLS, SNI). HTTP/2 connections are shared: flows multiplex
onto one up to the origin's SETTINGS_MAX_CONCURRENT_STREAMS, then another is
opened. HTTP/1.1 connections serve one request at a time; idle ones are reused
most-recently-used first. At most ``max_per_host`` connections per key; a flow
that finds them all busy waits, and the wait counts towards the connect
timeout. A connection is health-checked when it is checked out, and closed
after ``idle_timeout`` unused. There is no active probing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
from dataclasses import dataclass
from typing import Protocol

from mitm_proxy.errors import UpstreamConnectError, UpstreamTimeout, UpstreamTLSError
from mitm_proxy.http.h1 import H1Upstream
from mitm_proxy.http.h2 import H2Upstream
from mitm_proxy.streams import ByteStream, PlainStream
from mitm_proxy.tls.upstream import ALPN_ANY, ALPN_H1, UpstreamContexts

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Target:
    host: str
    port: int
    tls: bool = True
    sni: str | None = None  # what the client asked for; defaults to host

    @property
    def server_hostname(self) -> str:
        return self.sni or self.host


@dataclass
class Connected:
    stream: ByteStream
    alpn: str | None
    address: tuple[str, int] | None = None


class Connector(Protocol):
    async def __call__(
        self, target: Target, alpn: tuple[str, ...], timeout: float
    ) -> Connected: ...


class TCPConnector:
    """Open a TCP (and, for TLS targets, verified TLS) connection."""

    def __init__(self, contexts: UpstreamContexts, *, write_timeout: float | None = None) -> None:
        self.contexts = contexts
        self.write_timeout = write_timeout  # an origin that stops reading fails the write

    async def __call__(self, target: Target, alpn: tuple[str, ...], timeout: float) -> Connected:
        ctx: ssl.SSLContext | None = self.contexts.for_alpn(alpn) if target.tls else None
        try:
            async with asyncio.timeout(timeout):
                reader, writer = await asyncio.open_connection(
                    target.host,
                    target.port,
                    ssl=ctx,
                    server_hostname=target.server_hostname if ctx else None,
                )
        except TimeoutError:
            raise UpstreamTimeout(f"connecting to {target.host}:{target.port} timed out") from None
        except ssl.SSLCertVerificationError as e:
            raise UpstreamTLSError(
                f"{target.server_hostname}: certificate verification failed: {e.verify_message}"
            ) from e
        except ssl.SSLError as e:
            raise UpstreamTLSError(
                f"{target.server_hostname}: TLS handshake failed: {getattr(e, 'reason', None) or e}"
            ) from e
        except OSError as e:
            raise UpstreamConnectError(
                f"cannot connect to {target.host}:{target.port}: {e.strerror or e}"
            ) from e
        negotiated = None
        ssl_object = writer.get_extra_info("ssl_object")
        if ssl_object is not None:
            negotiated = ssl_object.selected_alpn_protocol()
        peer = writer.get_extra_info("peername")
        address = (str(peer[0]), int(peer[1])) if isinstance(peer, tuple) else None
        stream = PlainStream(reader, writer, write_timeout=self.write_timeout)
        return Connected(stream, negotiated, address)


Upstream = H1Upstream | H2Upstream


@dataclass
class Lease:
    connection: Upstream
    reused: bool
    address: tuple[str, int] | None = None


class ConnectionPool:
    def __init__(
        self,
        connector: Connector,
        *,
        max_per_host: int = 6,
        idle_timeout: float = 60.0,
        connect_timeout: float = 10.0,
        read_timeout: float = 120.0,
    ) -> None:
        self.connector = connector
        self.max_per_host = max_per_host
        self.idle_timeout = idle_timeout
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self._connections: dict[Target, list[Upstream]] = {}
        self._addresses: dict[int, tuple[str, int] | None] = {}
        self._pending: dict[Target, int] = {}
        self._last_used: dict[int, float] = {}
        # What each target negotiated last: while one connection to a target that may
        # speak h2 is being opened, others wait for it instead of opening their own.
        self._protocols: dict[Target, str] = {}
        # Replaced on every change; waiters hold the one current when they checked.
        # Pool decisions are synchronous between awaits, so no lock is needed.
        self._changed = asyncio.Event()
        self._closing: set[asyncio.Task[None]] = set()
        self._sweeper: asyncio.Task[None] | None = None
        self._closed = False

    def counts(self) -> dict[str, int]:
        """Open connections by protocol (for metrics)."""
        out = {"http/1.1": 0, "h2": 0}
        for conns in self._connections.values():
            for conn in conns:
                out[conn.protocol] += 1
        return out

    def _signal(self) -> None:
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    async def _notify(self) -> None:
        self._signal()

    def _close_later(self, conn: Upstream) -> None:
        task = asyncio.create_task(conn.close())
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def acquire(self, target: Target) -> Lease:
        """A connection with room for one more request to target."""
        if self._closed:
            raise UpstreamConnectError("the proxy is shutting down")
        self._start_sweeper()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.connect_timeout
        while True:
            conns = self._connections.setdefault(target, [])
            for conn in [c for c in conns if not c.healthy() and c.active == 0]:
                conns.remove(conn)
                self._close_later(conn)
            h2_free = [c for c in conns if isinstance(c, H2Upstream) and c.can_take_request()]
            if h2_free:
                return self._lease(h2_free[0], reused=True)
            idle = [c for c in conns if isinstance(c, H1Upstream) and c.can_take_request()]
            if idle:
                chosen = max(idle, key=lambda c: self._last_used.get(id(c), 0.0))
                chosen.busy = True
                return self._lease(chosen, reused=True)
            pending = self._pending.get(target, 0)
            may_share = self._protocols.get(target) != "http/1.1"
            if len(conns) + pending < self.max_per_host and not (pending and may_share):
                self._pending[target] = pending + 1
                break
            remaining = deadline - loop.time()
            changed = self._changed
            try:
                if remaining <= 0:
                    raise TimeoutError
                await asyncio.wait_for(changed.wait(), remaining)
            except TimeoutError:
                raise UpstreamTimeout(
                    f"no free connection to {target.host}:{target.port}"
                ) from None
        try:
            conn, address = await self._open(target, max(0.001, deadline - loop.time()), ALPN_ANY)
        except BaseException:
            self._pending[target] -= 1
            self._signal()
            raise
        self._pending[target] -= 1
        self._protocols[target] = conn.protocol
        if isinstance(conn, H1Upstream):
            conn.busy = True
        self._connections.setdefault(target, []).append(conn)
        self._addresses[id(conn)] = address
        self._signal()
        return self._lease(conn, reused=False)

    def _lease(self, conn: Upstream, *, reused: bool) -> Lease:
        self._last_used[id(conn)] = asyncio.get_running_loop().time()
        return Lease(conn, reused, self._addresses.get(id(conn)))

    async def _open(
        self, target: Target, timeout: float, alpn: tuple[str, ...]
    ) -> tuple[Upstream, tuple[str, int] | None]:
        connected = await self.connector(target, alpn, timeout)
        if connected.alpn == "h2" and target.tls and alpn != ALPN_H1:
            h2_conn = H2Upstream(
                connected.stream, read_timeout=self.read_timeout, on_capacity=self._notify
            )
            await h2_conn.start()
            conn: Upstream = h2_conn
        else:
            conn = H1Upstream(connected.stream, read_timeout=self.read_timeout, on_done=self._done)
        return conn, connected.address

    async def open_dedicated(self, target: Target) -> tuple[ByteStream, tuple[str, int] | None]:
        """An unpooled HTTP/1.1-only connection (WebSocket upgrades)."""
        connected = await self.connector(target, ALPN_H1, self.connect_timeout)
        return connected.stream, connected.address

    async def _done(self, conn: H1Upstream, reusable: bool) -> None:
        if reusable:
            self._last_used[id(conn)] = asyncio.get_running_loop().time()
        else:
            self._forget(conn)
        self._signal()

    def _forget(self, conn: Upstream) -> None:
        for conns in self._connections.values():
            if conn in conns:
                conns.remove(conn)
        self._addresses.pop(id(conn), None)
        self._last_used.pop(id(conn), None)

    def _start_sweeper(self) -> None:
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.create_task(self._sweep())

    async def _sweep(self) -> None:
        while True:
            # Read each time: a new configuration may change idle_timeout.
            await asyncio.sleep(max(0.05, min(self.idle_timeout / 2, 5.0)))
            await self.sweep_once()

    async def sweep_once(self) -> None:
        """Close connections idle for longer than idle_timeout, and dead ones."""
        now = asyncio.get_running_loop().time()
        stale: list[Upstream] = []
        for conns in self._connections.values():
            for conn in list(conns):
                idle_for = now - self._last_used.get(id(conn), now)
                if conn.active == 0 and (not conn.healthy() or idle_for >= self.idle_timeout):
                    conns.remove(conn)
                    stale.append(conn)
        for target in [
            t for t, c in self._connections.items() if not c and not self._pending.get(t)
        ]:
            del self._connections[target]
            self._protocols.pop(target, None)
        for conn in stale:
            self._addresses.pop(id(conn), None)
            self._last_used.pop(id(conn), None)
            with contextlib.suppress(OSError, ConnectionError):
                await conn.close()

    async def close(self) -> None:
        self._closed = True
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
        conns = [c for cs in self._connections.values() for c in cs]
        self._connections.clear()
        await asyncio.gather(*(c.close() for c in conns), *self._closing, return_exceptions=True)
        self._signal()
