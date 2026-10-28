"""Helpers shared by the tests: in-memory TLS handshakes and test CAs (no network)."""

from __future__ import annotations

import asyncio
import contextlib
import ssl

from mitm_proxy.tls.ca import CertificateAuthority, generate_root


def client_hello_bytes(server_hostname: str | None, alpn: list[str] | None = None) -> bytes:
    """The first flight a real ssl client sends."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if alpn:
        ctx.set_alpn_protocols(alpn)
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    obj = ctx.wrap_bio(incoming, outgoing, server_hostname=server_hostname)
    with contextlib.suppress(ssl.SSLWantReadError):
        obj.do_handshake()
    return outgoing.read()


def make_ca(common_name: str = "test CA") -> CertificateAuthority:
    cert, key = generate_root(common_name)
    return CertificateAuthority(cert, key, cache_size=4)


def trusting_client_context(
    ca: CertificateAuthority, alpn: list[str] | None = None
) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cadata=ca.cert_pem.decode())
    if alpn:
        ctx.set_alpn_protocols(alpn)
    return ctx


def handshake_in_memory(
    client_ctx: ssl.SSLContext, server_ctx: ssl.SSLContext, server_hostname: str
) -> tuple[ssl.SSLObject, ssl.SSLObject]:
    """Run a full handshake between two SSLObjects; raises ssl.SSLError on failure."""
    c_in, c_out, s_in, s_out = (ssl.MemoryBIO() for _ in range(4))
    client = client_ctx.wrap_bio(c_in, c_out, server_hostname=server_hostname)
    server = server_ctx.wrap_bio(s_in, s_out, server_side=True)
    done = {"client": False, "server": False}
    for _ in range(10):
        for name, obj in (("client", client), ("server", server)):
            if not done[name]:
                try:
                    obj.do_handshake()
                    done[name] = True
                except ssl.SSLWantReadError:
                    pass
        s_in.write(c_out.read())
        c_in.write(s_out.read())
        if all(done.values()):
            return client, server
    raise AssertionError("handshake did not finish")


class MemoryStream:
    """One end of an in-memory ByteStream pair (no backpressure, one reader)."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._eof = False
        self._closed = False
        self._ready = asyncio.Event()
        self.peer: MemoryStream | None = None
        self.written = 0

    def _feed(self, data: bytes) -> None:
        self._buffer += data
        self._ready.set()

    def _end(self) -> None:
        self._eof = True
        self._ready.set()

    async def read(self, n: int = 65536) -> bytes:
        while not self._buffer and not self._eof:
            self._ready.clear()
            await self._ready.wait()
        data = bytes(self._buffer[:n])
        del self._buffer[:n]
        return data

    async def write(self, data: bytes) -> None:
        if self._closed or self.peer is None or self.peer._closed:
            raise ConnectionResetError("memory stream closed")
        self.written += len(data)
        self.peer._feed(bytes(data))
        await asyncio.sleep(0)

    def write_eof(self) -> None:
        if self.peer is not None:
            self.peer._end()

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.write_eof()
            self._end()

    def is_closing(self) -> bool:
        return self._closed


def memory_pair() -> tuple[MemoryStream, MemoryStream]:
    a, b = MemoryStream(), MemoryStream()
    a.peer, b.peer = b, a
    return a, b
