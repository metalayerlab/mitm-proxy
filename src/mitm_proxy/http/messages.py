"""What the protocol layers hand to the exchange, and what they take back.

The h1 and h2 layers turn wire traffic into an IncomingRequest and answer through
a Responder; the upstream clients return an UpstreamResponse. The exchange in
between never sees h11 or h2 objects.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from mitm_proxy.http.flow import Request, Response
from mitm_proxy.http.headers import Headers
from mitm_proxy.streams import ByteStream


def _no_trailers() -> Headers | None:
    return None


@dataclass
class IncomingRequest:
    request: Request  # head only: the exchange decides what its body becomes
    body: AsyncIterator[bytes]  # de-framed chunks from the client
    has_body: bool
    declared_length: int | None = None
    trailers: Callable[[], Headers | None] = _no_trailers  # valid once body is exhausted
    accepts_trailers: bool = False  # the client sent TE: trailers
    # h1 only: the request asks for a WebSocket upgrade, and this switches the client
    # connection (sends 101 with these headers) and returns it for relaying.
    websocket: Callable[[Headers], Awaitable[ByteStream]] | None = None
    raw_headers: Headers = field(default_factory=Headers)  # as received, before normalization


class Responder(Protocol):
    protocol: str
    started: bool

    async def start(self, response: Response, headers: Headers, *, end: bool) -> None:
        """Send the status and (already framed) headers; end=True if no body follows."""
        ...

    async def data(self, chunk: bytes) -> None: ...

    async def end(self, trailers: Headers | None) -> None: ...


async def _nothing() -> None:
    return None


@dataclass
class UpstreamResponse:
    response: Response  # head only
    body: AsyncIterator[bytes]
    has_body: bool
    declared_length: int | None = None
    trailers: Callable[[], Headers | None] = _no_trailers
    # Abandon the rest of the body (the connection is then not reused).
    discard: Callable[[], Awaitable[None]] = _nothing
    # 101 Switching Protocols: the raw upstream stream, for relaying.
    switched: ByteStream | None = None
