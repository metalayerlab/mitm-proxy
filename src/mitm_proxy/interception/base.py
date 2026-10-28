"""The seam between "how did this connection get here" and everything else.

An InterceptionSource produces InterceptedConnections: a client stream and the
target it wants. Nothing downstream (classification, TLS, certificates, the
HTTP layers, middleware) knows which source produced one. Phase 1 has the
explicit-proxy source (a CONNECT line); Phase 2 adds a transparent source
(original destination + SNI), whose accept/reject do nothing because there is
no CONNECT to answer.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from mitm_proxy.streams import ByteStream


async def _nothing() -> None:
    return None


async def _nothing_status(status: int, message: str) -> None:
    return None


@dataclass
class InterceptedConnection:
    stream: ByteStream  # the client side; any bytes read past the preamble are replayed
    target_host: str
    target_port: int
    client_addr: tuple[str, int]
    # Tell the client the tunnel is up (CONNECT: 200) ...
    accept: Callable[[], Awaitable[None]] = _nothing
    # ... or that it failed (CONNECT: this status and a one-line reason, then close).
    reject: Callable[[int, str], Awaitable[None]] = _nothing_status


TunnelHandler = Callable[[InterceptedConnection], Awaitable[None]]


class InterceptionSource(Protocol):
    async def handle_client(self, stream: ByteStream, client_addr: tuple[str, int]) -> None:
        """Serve one accepted client connection, passing each tunnel to the tunnel handler."""
        ...
