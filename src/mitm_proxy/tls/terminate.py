"""TLS on top of any ByteStream, using ssl.SSLObject over memory BIOs.

The client's ClientHello has already been read (to classify the tunnel and pick
the certificate), and asyncio's ``start_tls`` would drop bytes already buffered.
So the handshake runs here instead: the peeked bytes are fed in first, then a
small pump moves bytes between the BIOs and the underlying stream. Every
operation on the SSLObject is synchronous; only the underlying I/O awaits, so
concurrent readers and writers on one event loop interleave safely.
"""

from __future__ import annotations

import contextlib
import ssl

from mitm_proxy.streams import READ_SIZE, ByteStream


class TLSStream:
    def __init__(
        self,
        raw: ByteStream,
        context: ssl.SSLContext,
        *,
        server_side: bool,
        server_hostname: str | None = None,
        initial: bytes = b"",
    ) -> None:
        self.raw = raw
        self._incoming = ssl.MemoryBIO()
        self._outgoing = ssl.MemoryBIO()
        self._obj = context.wrap_bio(
            self._incoming,
            self._outgoing,
            server_side=server_side,
            server_hostname=server_hostname,
        )
        if initial:
            self._incoming.write(initial)
        self._closed = False

    async def _flush(self) -> None:
        data = self._outgoing.read()
        if data:
            await self.raw.write(data)

    async def _fill(self) -> bool:
        """Feed more raw bytes in; False at end of stream."""
        data = await self.raw.read(READ_SIZE)
        if not data:
            self._incoming.write_eof()
            return False
        self._incoming.write(data)
        return True

    async def handshake(self) -> None:
        """Complete the handshake; ssl.SSLError or ConnectionError if it fails."""
        while True:
            try:
                self._obj.do_handshake()
                break
            except ssl.SSLWantReadError:
                await self._flush()
                if not await self._fill():
                    raise ConnectionResetError(
                        "connection closed during the TLS handshake"
                    ) from None
            except ssl.SSLError:
                # Send our alert to the peer before giving up.
                await self._flush_quietly()
                raise
        await self._flush()

    async def _flush_quietly(self) -> None:
        with contextlib.suppress(OSError, ConnectionError):
            await self._flush()

    @property
    def alpn(self) -> str | None:
        return self._obj.selected_alpn_protocol()

    @property
    def version(self) -> str | None:
        return self._obj.version()

    @property
    def cipher(self) -> str | None:
        cipher = self._obj.cipher()
        return cipher[0] if cipher else None

    async def read(self, n: int = READ_SIZE) -> bytes:
        while True:
            try:
                data = self._obj.read(n)
            except ssl.SSLWantReadError:
                await self._flush()  # e.g. a TLS 1.3 key update reply
                if not await self._fill():
                    return b""
                continue
            except (ssl.SSLZeroReturnError, ssl.SSLEOFError):
                return b""
            if self._outgoing.pending:
                await self._flush()
            return data

    async def write(self, data: bytes) -> None:
        if self._closed:
            raise ConnectionResetError("TLS stream is closed")
        view = memoryview(data)
        while view:
            written = self._obj.write(view)
            view = view[written:]
            await self._flush()

    def write_eof(self) -> None:
        """TLS has no half-close; the stream is closed as a whole instead."""

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(ssl.SSLError, ValueError):
            self._obj.unwrap()
        await self._flush_quietly()
        await self.raw.close()

    def is_closing(self) -> bool:
        return self._closed or self.raw.is_closing()
