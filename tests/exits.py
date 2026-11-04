"""Fake exits for design 0013's tests: an HTTP CONNECT proxy and a SOCKS5 server, on loopback.

Neither resolves anything. Each maps the ``host:port`` it is asked for to a local port from its
``routes`` table (a test origin), the way a real exit would resolve and connect inside its
tunnel. Nothing here reaches the network.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import ssl
import struct
from dataclasses import dataclass, field

from mitm_proxy.tls.ca import CertificateAuthority

# Made up for these tests; never a real credential.
TEST_LOGIN = "tester:not-a-real-password"


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (OSError, ConnectionError):
        pass
    finally:
        with contextlib.suppress(OSError, ConnectionError, RuntimeError):
            writer.close()


async def _relay(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, port: int | None
) -> None:
    if port is None:
        writer.close()
        return
    up_reader, up_writer = await asyncio.open_connection("127.0.0.1", port)
    await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))


@dataclass
class _Server:
    routes: dict[str, int] = field(default_factory=dict)  # "host:port" -> a local port
    server: asyncio.Server | None = None
    tasks: set[asyncio.Task[None]] = field(default_factory=set)

    @property
    def port(self) -> int:
        assert self.server is not None
        return int(self.server.sockets[0].getsockname()[1])

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        return self.port

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.tasks.add(task)
        try:
            await self.handle(reader, writer)
        except (OSError, ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.tasks.discard(task)
            writer.close()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


@dataclass
class FakeHTTPExit(_Server):
    """An HTTP proxy: CONNECT relays to the mapped port; absolute form is answered here."""

    status: int = 200
    login: str | None = None  # a required Proxy-Authorization login
    silent: bool = False  # never answer
    heads: list[str] = field(default_factory=list)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            self.heads.append(head)
            if self.silent:
                await asyncio.sleep(60)
            method, target, _ = head.split("\r\n", 1)[0].split(" ", 2)
            if self.login is not None:
                wanted = "Basic " + base64.b64encode(self.login.encode()).decode()
                if f"Proxy-Authorization: {wanted}".lower() not in head.lower():
                    writer.write(b"HTTP/1.1 407 Login Please\r\nContent-Length: 0\r\n\r\n")
                    await writer.drain()
                    return
            if method == "CONNECT":
                if self.status != 200:
                    writer.write(f"HTTP/1.1 {self.status} No\r\nContent-Length: 0\r\n\r\n".encode())
                    await writer.drain()
                    return
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
                await _relay(reader, writer, self.routes.get(target))
                return
            body = f"exit saw {method} {target}".encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            await writer.drain()


@dataclass
class SocksRequest:
    address_type: int
    host: str
    port: int


@dataclass
class FakeSocksExit(_Server):
    """A SOCKS5 server (RFC 1928, and RFC 1929 when ``login`` is set)."""

    login: str | None = None
    reply: int = 0
    methods: list[bytes] = field(default_factory=list)
    logins: list[tuple[str, str]] = field(default_factory=list)
    requests: list[SocksRequest] = field(default_factory=list)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        version, count = await reader.readexactly(2)
        offered = await reader.readexactly(count)
        self.methods.append(offered)
        method = 0x02 if self.login is not None else 0x00
        if version != 5 or method not in offered:
            writer.write(bytes((5, 0xFF)))
            await writer.drain()
            return
        writer.write(bytes((5, method)))
        await writer.drain()
        if self.login is not None:
            _, user_length = await reader.readexactly(2)
            user = (await reader.readexactly(user_length)).decode()
            (password_length,) = await reader.readexactly(1)
            password = (await reader.readexactly(password_length)).decode()
            self.logins.append((user, password))
            ok = f"{user}:{password}" == self.login
            writer.write(bytes((1, 0 if ok else 1)))
            await writer.drain()
            if not ok:
                return
        _, command, _, address_type = await reader.readexactly(4)
        if address_type == 1:
            host = str(ipaddress.IPv4Address(await reader.readexactly(4)))
        elif address_type == 4:
            host = str(ipaddress.IPv6Address(await reader.readexactly(16)))
        else:
            (length,) = await reader.readexactly(1)
            host = (await reader.readexactly(length)).decode()
        (port,) = struct.unpack("!H", await reader.readexactly(2))
        self.requests.append(SocksRequest(address_type, host, port))
        bound = bytes((1, 0, 0, 0, 0)) + struct.pack("!H", 0)
        writer.write(bytes((5, self.reply if command == 1 else 7, 0)) + bound)
        await writer.drain()
        if self.reply == 0:
            key = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
            await _relay(reader, writer, self.routes.get(key))


@dataclass
class TestOrigin(_Server):
    """An HTTP/1.1 origin on loopback, with TLS for ``host`` when a CA is given."""

    __test__ = False  # not a pytest class

    ca: CertificateAuthority | None = None
    host: str = "origin.test"
    requests: list[str] = field(default_factory=list)

    async def start(self) -> int:
        ctx: ssl.SSLContext | None = None
        if self.ca is not None:
            ctx = self.ca.server_context(self.host)
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0, ssl=ctx)
        return self.port

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            self.requests.append(head.split("\r\n", 1)[0])
            body = b"hello from the origin"
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            await writer.drain()
