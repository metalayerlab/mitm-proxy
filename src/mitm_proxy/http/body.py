"""Bodies, as middleware sees them: async-iterable chunks, de-framed but not content-decoded.

The interface is chunk-streaming from the start; Phase 1 buffers (one chunk per
body), and a streaming implementation can replace BufferedBodyStream later
without any middleware changing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable


@runtime_checkable
class BodyStream(Protocol):
    def __aiter__(self) -> AsyncIterator[bytes]: ...

    async def read_all(self) -> bytes: ...

    def is_complete(self) -> bool: ...


class BufferedBodyStream:
    """A whole body in memory. Iterating yields it as one chunk (nothing, if empty)."""

    def __init__(self, data: bytes = b"") -> None:
        self._data = bytes(data)

    async def _chunks(self) -> AsyncIterator[bytes]:
        if self._data:
            yield self._data

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._chunks()

    async def read_all(self) -> bytes:
        return self._data

    def is_complete(self) -> bool:
        return True

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"BufferedBodyStream({len(self._data)} bytes)"


class PassthroughBody:
    """A body forwarded as it arrives, with no middleware involved (the fast path).

    It can be iterated once; ``read_all`` refuses, since nothing may buffer it.
    """

    def __init__(self, source: AsyncIterator[bytes]) -> None:
        self._source = source
        self._used = False
        self._done = False

    async def _chunks(self) -> AsyncIterator[bytes]:
        async for chunk in self._source:
            if chunk:
                yield chunk
        self._done = True

    def __aiter__(self) -> AsyncIterator[bytes]:
        if self._used:
            raise RuntimeError("a passthrough body can only be iterated once")
        self._used = True
        return self._chunks()

    async def read_all(self) -> bytes:
        raise RuntimeError("a passthrough body is not buffered")

    def is_complete(self) -> bool:
        return self._done


async def empty() -> AsyncIterator[bytes]:
    """An async iterator with no chunks."""
    return
    yield  # pragma: no cover - makes this an async generator
