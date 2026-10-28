"""The Flow: one logical request/response exchange (one h2 stream, or one h1 request).

This is the unit middleware works on. It is protocol-agnostic: headers are
normalized, h2 pseudo-headers are fields, and bodies are BodyStreams. The data
is plain (no sockets or loop objects outside the bodies), so a serializer can be
added later without changing its shape.
"""

from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from mitm_proxy.http.body import BodyStream, BufferedBodyStream
from mitm_proxy.http.headers import Headers


class FlowType(enum.Enum):
    HTTP = "http"
    WEBSOCKET = "websocket"
    # RAW_TCP is reserved for Phase 2 (transparent interception of non-HTTP traffic).


@dataclass
class ClientConn:
    """The client leg: who connected, and what TLS they negotiated (None for plain HTTP)."""

    address: tuple[str, int]
    sni: str | None = None
    alpn_offered: tuple[str, ...] = ()
    tls_version: str | None = None
    cipher: str | None = None


@dataclass
class ServerConn:
    """The upstream leg. Pooled, so possibly shared by several flows."""

    host: str
    port: int
    protocol: str = "http/1.1"  # this leg's own ALPN result
    tls: bool = True
    sni: str | None = None
    address: tuple[str, int] | None = None


@dataclass
class Request:
    method: str
    scheme: str
    authority: str  # host[:port], as the client sent it (Host or :authority)
    path: str
    headers: Headers = field(default_factory=Headers)
    body: BodyStream = field(default_factory=BufferedBodyStream)
    trailers: Headers | None = None

    @property
    def host(self) -> str:
        host, _ = split_authority(self.authority, 443 if self.scheme == "https" else 80)
        return host

    @property
    def port(self) -> int:
        _, port = split_authority(self.authority, 443 if self.scheme == "https" else 80)
        return port


@dataclass
class Response:
    status: int
    headers: Headers = field(default_factory=Headers)
    body: BodyStream = field(default_factory=BufferedBodyStream)
    reason: str = ""
    trailers: Headers | None = None

    @classmethod
    def make(cls, status: int, body: bytes = b"", headers: Headers | None = None) -> Response:
        """A response a middleware can send itself (e.g. a cache hit, with Action.STOP)."""
        hdrs = headers.copy() if headers is not None else Headers()
        if body and "content-type" not in hdrs:
            hdrs.set("content-type", "text/plain; charset=utf-8")
        return cls(status=status, headers=hdrs, body=BufferedBodyStream(body))


@dataclass
class Timings:
    """When each stage of a flow happened, on the ``time.monotonic()`` clock.

    Filled in by the proxy as the flow progresses; middleware reads it and does
    not write it. A stage that didn't happen (a DENY, a STOP with a response, an
    upstream error) stays None, and so do the durations derived from it.
    """

    started: float  # the request headers arrived
    request_complete: float | None = None  # the request body was read (buffered or passed on)
    upstream_requested: float | None = None  # a connection was asked of the pool
    upstream_acquired: float | None = None  # ... and leased (or opened)
    request_sent: float | None = None  # the request, head and body, went upstream
    response_headers: float | None = None  # the origin's response headers arrived
    complete: float | None = None  # the last byte went to the client
    connection_reused: bool = False
    middleware: dict[str, float] = field(default_factory=dict)  # seconds in each one's hooks

    @property
    def connect(self) -> float | None:
        """Waiting for an upstream connection: pooled (about zero) or a new TCP and TLS one."""
        return _between(self.upstream_requested, self.upstream_acquired)

    @property
    def ttfb(self) -> float | None:
        """The origin's share: from the request being sent to its response headers."""
        return _between(self.request_sent, self.response_headers)

    @property
    def middleware_total(self) -> float:
        return sum(self.middleware.values())

    @property
    def total(self) -> float | None:
        return _between(self.started, self.complete)


def _between(start: float | None, end: float | None) -> float | None:
    return None if start is None or end is None else end - start


class Flow:
    def __init__(
        self,
        *,
        type: FlowType,
        protocol: str,
        client_conn: ClientConn,
        request: Request,
        server_conn: ServerConn | None = None,
    ) -> None:
        self.id = uuid.uuid4().hex
        self.type = type
        self._protocol = protocol
        self.client_conn = client_conn
        self.server_conn = server_conn
        self.request = request
        self.response: Response | None = None
        self.metadata: dict[str, Any] = {}
        self.timings = Timings(started=time.monotonic())

    @property
    def protocol(self) -> str:
        """The client leg's protocol, "http/1.1" or "h2". Read-only."""
        return self._protocol

    @property
    def host(self) -> str:
        return self.request.host

    @property
    def port(self) -> int:
        return self.request.port

    @property
    def method(self) -> str:
        return self.request.method

    def __repr__(self) -> str:
        r = self.request
        return f"<Flow {self.id} {self.protocol} {r.method} {r.scheme}://{r.authority}{r.path}>"


def split_authority(authority: str, default_port: int) -> tuple[str, int]:
    """("host", port) from "host", "host:port", "[v6]" or "[v6]:port"."""
    text = authority.strip()
    if text.startswith("["):
        end = text.find("]")
        if end == -1:
            return text, default_port
        host, rest = text[1:end], text[end + 1 :]
        if rest.startswith(":") and rest[1:].isdigit():
            return host, int(rest[1:])
        return host, default_port
    host, sep, port = text.rpartition(":")
    if sep and port.isdigit() and ":" not in host:
        return host, int(port)
    return text, default_port
