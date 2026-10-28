"""Byte streams: the one shape every layer reads and writes.

The HTTP layers and the relay work on a ByteStream and never know whether it is
a plain socket, TLS on top of one, or (in tests) memory. ``write`` awaits
``drain()``, so a slow peer slows the writer down: that is the backpressure.
A peer that stops reading altogether is a different matter: with a write
timeout, a write that makes no progress for that long fails, so nothing above
(HTTP/1.1, HTTP/2, the relay) can wait on a socket forever.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable
from typing import Protocol

READ_SIZE = 65536
# Writes go out in pieces of this size, each with its own write timeout: the timeout
# is for a peer that makes no progress, not for one large write (a buffered body).
WRITE_PIECE = 65536


class ByteStream(Protocol):
    async def read(self, n: int = READ_SIZE) -> bytes:
        """Up to n bytes; b"" at end of stream."""
        ...

    async def write(self, data: bytes) -> None:
        """Write and wait until the peer can take more."""
        ...

    def write_eof(self) -> None:
        """Half-close the sending side, where the stream supports it."""
        ...

    async def close(self) -> None: ...

    def is_closing(self) -> bool: ...


class PlainStream:
    """An asyncio reader/writer pair, with optional bytes to replay before reading."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        prefix: bytes = b"",
        *,
        write_timeout: float | None = None,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self._prefix = prefix
        self.write_timeout = write_timeout  # None: wait for the peer as long as it takes

    async def read(self, n: int = READ_SIZE) -> bytes:
        if self._prefix:
            data, self._prefix = self._prefix[:n], self._prefix[n:]
            return data
        return await self.reader.read(n)

    async def write(self, data: bytes) -> None:
        view = memoryview(data)
        for start in range(0, len(view), WRITE_PIECE):
            self.writer.write(view[start : start + WRITE_PIECE])
            await self._bounded(self.writer.drain())

    async def _bounded(self, waiting: Awaitable[None]) -> None:
        """Await waiting under the write timeout; on expiry, drop the connection."""
        if self.write_timeout is None:
            await waiting
            return
        timer = asyncio.timeout(self.write_timeout)
        try:
            async with timer:
                await waiting
        except TimeoutError:
            if not timer.expired():
                raise  # the OS's own ETIMEDOUT, not ours: pass it on as it is
            # Half-written data leaves the stream unusable: drop the connection.
            self.writer.transport.abort()
            raise ConnectionResetError(
                f"the peer stopped reading (no progress for {self.write_timeout:g}s)"
            ) from None

    def write_eof(self) -> None:
        if self.writer.can_write_eof() and not self.writer.is_closing():
            with contextlib.suppress(OSError, RuntimeError):
                self.writer.write_eof()

    async def close(self) -> None:
        """Close, after sending what's queued, but never wait longer than the write timeout."""
        self.writer.close()
        try:
            with contextlib.suppress(OSError, ConnectionError):
                await self._bounded(self.writer.wait_closed())
        except asyncio.CancelledError:
            self.writer.transport.abort()  # shutting down: don't leave the socket behind
            raise

    def is_closing(self) -> bool:
        return self.writer.is_closing() or self.reader.at_eof()

    def peer(self) -> tuple[str, int]:
        address = self.writer.get_extra_info("peername")
        if isinstance(address, tuple) and len(address) >= 2:
            return str(address[0]), int(address[1])
        return ("", 0)


class PrefixedStream:
    """Another ByteStream with bytes put back in front of it."""

    def __init__(self, inner: ByteStream, prefix: bytes) -> None:
        self.inner = inner
        self._prefix = prefix

    async def read(self, n: int = READ_SIZE) -> bytes:
        if self._prefix:
            data, self._prefix = self._prefix[:n], self._prefix[n:]
            return data
        return await self.inner.read(n)

    async def write(self, data: bytes) -> None:
        await self.inner.write(data)

    def write_eof(self) -> None:
        self.inner.write_eof()

    async def close(self) -> None:
        await self.inner.close()

    def is_closing(self) -> bool:
        return self.inner.is_closing()


def prepend(stream: ByteStream, data: bytes) -> ByteStream:
    return PrefixedStream(stream, data) if data else stream
