"""Connectors that chain through an exit (design 0013, section 2).

Two connectors beside ``TCPConnector``, chosen by the exit's kind:

- ``HTTPConnectConnector``: TCP to an HTTP proxy, then ``CONNECT host:port`` (with ``Host``, and
  ``Proxy-Authorization`` when the exit has a login), expecting a ``2xx``; then, for a TLS target,
  TLS to the origin inside that tunnel, verified as always, with the origin's name as SNI. Plain
  HTTP requests (not a relayed tunnel) skip the ``CONNECT`` and go to the exit in absolute form
  (``H1Upstream``'s ``forward_proxy``).
- ``Socks5Connector``: SOCKS5 (RFC 1928) with no authentication, or username and password (RFC
  1929) when the exit has a login, and a ``CONNECT`` with the domain-name address type, so that the
  exit resolves the name (``socks5h``).

**The proxy never resolves a routed site's name.** It resolves only the exit's own host, and passes
the site's name to the exit as written. An IP literal is passed as it is.

``RoutingConnector`` picks the connector for each target: its exit's kind, or the direct
``TCPConnector`` when the target has no exit (routing off, or ``direct``).

Errors are the pool's: ``UpstreamConnectError`` (the exit can't be reached, or it refused),
``UpstreamTimeout`` and ``UpstreamTLSError``. Messages name the exit, never its login.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import ssl
import struct

from mitm_proxy.errors import UpstreamConnectError, UpstreamTimeout
from mitm_proxy.routing import Exit, is_ip_literal
from mitm_proxy.streams import PlainStream
from mitm_proxy.tls.upstream import UpstreamContexts
from mitm_proxy.upstream.pool import (
    Connected,
    Target,
    TCPConnector,
    proxy_authorization,
    tls_error,
)

log = logging.getLogger(__name__)

MAX_CONNECT_HEAD = 16 * 1024  # an exit's answer to CONNECT: a status line and a few headers

# RFC 1928, section 6: what each SOCKS5 reply code means.
SOCKS5_REPLIES = {
    1: "general SOCKS server failure",
    2: "connection not allowed by ruleset",
    3: "network unreachable",
    4: "host unreachable",
    5: "connection refused",
    6: "TTL expired",
    7: "command not supported",
    8: "address type not supported",
}


def _authority(host: str, port: int) -> str:
    """``host:port``, with an IPv6 literal in brackets."""
    bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    return f"[{bare}]:{port}" if ":" in bare else f"{bare}:{port}"


def _check_host(exit_: Exit, host: str) -> None:
    """A site's name goes onto the wire to the exit: printable ASCII, no spaces, at most 255."""
    if not host or len(host) > 255 or not all("!" <= c <= "~" for c in host):
        raise UpstreamConnectError(f"exit {exit_.name}: not a host name the exit can be sent")


def _printable(text: str, limit: int = 80) -> str:
    return "".join(c for c in text if " " <= c <= "~")[:limit]


class _ExitConnector:
    """What both chained connectors share: reaching the exit, and TLS inside the tunnel."""

    def __init__(self, contexts: UpstreamContexts, *, write_timeout: float | None = None) -> None:
        self.contexts = contexts
        self.write_timeout = write_timeout

    async def __call__(self, target: Target, alpn: tuple[str, ...], timeout: float) -> Connected:
        exit_ = target.via
        if exit_ is None:  # pragma: no cover - RoutingConnector sends only targets with an exit
            raise UpstreamConnectError("no exit for this connection")
        _check_host(exit_, target.host)
        try:
            async with asyncio.timeout(timeout):
                reader, writer = await self._open(exit_)
                try:
                    await self._handshake(exit_, target, reader, writer)
                    negotiated = None
                    if target.tls:
                        ctx = self.contexts.for_alpn(alpn)
                        await writer.start_tls(ctx, server_hostname=target.server_hostname)
                        ssl_object = writer.get_extra_info("ssl_object")
                        if ssl_object is not None:
                            negotiated = ssl_object.selected_alpn_protocol()
                except BaseException:
                    writer.transport.abort()
                    raise
        except TimeoutError:
            raise UpstreamTimeout(
                f"connecting to {_authority(target.host, target.port)} through exit "
                f"{exit_.name} timed out"
            ) from None
        except ssl.SSLError as e:
            raise tls_error(target, e) from e
        except (OSError, asyncio.IncompleteReadError) as e:
            raise UpstreamConnectError(
                f"exit {exit_.name}: the connection failed: {getattr(e, 'strerror', None) or e}"
            ) from e
        # The origin's address is the exit's to know: the proxy never resolved its name.
        stream = PlainStream(reader, writer, write_timeout=self.write_timeout)
        return Connected(stream, negotiated, None)

    async def _open(self, exit_: Exit) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """TCP to the exit: the one name the proxy resolves when routing is on."""
        try:
            return await asyncio.open_connection(exit_.host, exit_.port)
        except OSError as e:
            raise UpstreamConnectError(
                f"cannot connect to exit {exit_.name}: {e.strerror or e}"
            ) from e

    async def _handshake(
        self,
        exit_: Exit,
        target: Target,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        raise NotImplementedError  # pragma: no cover - each connector has its own


class HTTPConnectConnector(_ExitConnector):
    """Through an HTTP proxy exit: ``CONNECT`` for TLS and tunnels, absolute form for plain HTTP."""

    async def _handshake(
        self,
        exit_: Exit,
        target: Target,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if target.absolute_form:
            return  # plain HTTP: each request names its origin (H1Upstream's forward_proxy)
        authority = _authority(target.host, target.port)
        lines = [f"CONNECT {authority} HTTP/1.1", f"Host: {authority}"]
        login = proxy_authorization(exit_)
        if login is not None:
            lines.append(f"Proxy-Authorization: {login}")
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        await writer.drain()
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except asyncio.LimitOverrunError:
            raise UpstreamConnectError(
                f"exit {exit_.name}: its answer to CONNECT is too long"
            ) from None
        except asyncio.IncompleteReadError:
            raise UpstreamConnectError(
                f"exit {exit_.name}: closed the connection without answering CONNECT"
            ) from None
        if len(head) > MAX_CONNECT_HEAD:
            raise UpstreamConnectError(f"exit {exit_.name}: its answer to CONNECT is too long")
        status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = status_line.split(" ", 2)
        if len(parts) < 2 or not parts[0].startswith("HTTP/1.") or not parts[1].isdigit():
            raise UpstreamConnectError(f"exit {exit_.name}: not an HTTP answer to CONNECT")
        status = int(parts[1])
        if not 200 <= status < 300:
            reason = _printable(parts[2]) if len(parts) > 2 else ""
            hint = ""
            if status == 407:
                hint = (
                    " (it wants a login: check its auth)" if login is None else " (login refused)"
                )
            answer = f"{status} {reason}".rstrip()
            raise UpstreamConnectError(
                f"exit {exit_.name} refused CONNECT to {authority}: {answer}{hint}"
            )


class Socks5Connector(_ExitConnector):
    """Through a SOCKS5 exit, names resolved by the exit (``socks5h``)."""

    async def _handshake(
        self,
        exit_: Exit,
        target: Target,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        credentials = exit_.credentials()
        method = 0x02 if credentials is not None else 0x00  # username/password, or none
        writer.write(bytes((5, 1, method)))
        await writer.drain()
        version, chosen = await reader.readexactly(2)
        if version != 5:
            raise UpstreamConnectError(f"exit {exit_.name}: not a SOCKS5 server")
        if chosen != method:
            wanted = "a login" if credentials is None else "no login, or another kind"
            raise UpstreamConnectError(
                f"exit {exit_.name}: refused the SOCKS5 authentication offered (it wants {wanted})"
            )
        if credentials is not None:
            user, password = (part.encode() for part in credentials)
            writer.write(bytes((1, len(user))) + user + bytes((len(password),)) + password)
            await writer.drain()
            _, status = await reader.readexactly(2)
            if status != 0:
                raise UpstreamConnectError(f"exit {exit_.name}: refused the SOCKS5 login")
        writer.write(
            bytes((5, 1, 0)) + _socks_address(target.host) + struct.pack("!H", target.port)
        )
        await writer.drain()
        version, reply, _, address_type = await reader.readexactly(4)
        if version != 5:
            raise UpstreamConnectError(f"exit {exit_.name}: not a SOCKS5 server")
        if reply != 0:
            meaning = SOCKS5_REPLIES.get(reply, f"reply {reply}")
            raise UpstreamConnectError(
                f"exit {exit_.name} refused CONNECT to "
                f"{_authority(target.host, target.port)}: {meaning}"
            )
        # The bound address: read past it. Nothing uses it.
        if address_type == 1:
            await reader.readexactly(4 + 2)
        elif address_type == 4:
            await reader.readexactly(16 + 2)
        elif address_type == 3:
            (length,) = await reader.readexactly(1)
            await reader.readexactly(length + 2)
        else:
            raise UpstreamConnectError(f"exit {exit_.name}: a malformed SOCKS5 reply")


def _socks_address(host: str) -> bytes:
    """DST.ADDR (RFC 1928, section 5): an IP literal as it is, any name as a domain name, which
    the exit resolves. The name is never resolved here."""
    if is_ip_literal(host):
        address = ipaddress.ip_address(host.strip("[]"))
        return bytes((1 if address.version == 4 else 4,)) + address.packed
    name = host.encode("ascii")  # _check_host has made sure it is printable ASCII
    return bytes((3, len(name))) + name


class RoutingConnector(TCPConnector):
    """The proxy's connector: the target's exit decides how to connect (design 0013).

    No exit: a direct connection, exactly ``TCPConnector``'s (routing off, or ``direct`` chosen).
    An exit: the connector for its kind. There is never a fallback from one to another.
    """

    def __init__(self, contexts: UpstreamContexts, *, write_timeout: float | None = None) -> None:
        self.http = HTTPConnectConnector(contexts, write_timeout=write_timeout)
        self.socks5 = Socks5Connector(contexts, write_timeout=write_timeout)
        super().__init__(contexts, write_timeout=write_timeout)

    @property
    def write_timeout(self) -> float | None:
        return self._write_timeout

    @write_timeout.setter
    def write_timeout(self, value: float | None) -> None:
        # One setting for every path, as for the direct one: a reload reaches them all.
        self._write_timeout = value
        self.http.write_timeout = value
        self.socks5.write_timeout = value

    async def __call__(self, target: Target, alpn: tuple[str, ...], timeout: float) -> Connected:
        if target.via is None:
            return await super().__call__(target, alpn, timeout)
        chained = self.http if target.via.kind == "http" else self.socks5
        return await chained(target, alpn, timeout)
