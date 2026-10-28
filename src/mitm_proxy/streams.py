"""Byte streams: the one shape every layer reads and writes.

The HTTP layers and the relay work on a ByteStream and never know whether it is
a plain socket, TLS on top of one, or (in tests) memory. ``write`` awaits
``drain()``, so a slow peer slows the writer down: that is the backpressure.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Protocol

READ_SIZE = 65536


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
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, prefix: bytes = b""
    ) -> None:
        self.reader = reader
        self.writer = writer
        self._prefix = prefix

    async def read(self, n: int = READ_SIZE) -> bytes:
        if self._prefix:
            data, self._prefix = self._prefix[:n], self._prefix[n:]
            return data
        return await self.reader.read(n)

    async def write(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    def write_eof(self) -> None:
        if self.writer.can_write_eof() and not self.writer.is_closing():
            with contextlib.suppress(OSError, RuntimeError):
                self.writer.write_eof()

    async def close(self) -> None:
        self.writer.close()
        with contextlib.suppress(OSError, ConnectionError, asyncio.CancelledError):
            await self.writer.wait_closed()

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
