"""Raw bidirectional relay: bytes in, bytes out, nothing inspected.

Used for tunnels that aren't intercepted (out of scope, not TLS) and for
WebSocket after the upgrade. Each direction awaits the write before reading
more, so a slow side slows the other. A half-close is passed on, and the relay
ends when both directions are done, or when neither has moved a byte for the
idle timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from mitm_proxy.streams import READ_SIZE, ByteStream


async def relay(a: ByteStream, b: ByteStream, *, idle_timeout: float) -> tuple[int, int]:
    """Copy a->b and b->a until both are done; returns (bytes a->b, bytes b->a). Closes both."""
    loop = asyncio.get_running_loop()
    last_activity = loop.time()
    counts = [0, 0]

    async def pipe(src: ByteStream, dst: ByteStream, index: int) -> None:
        nonlocal last_activity
        try:
            while True:
                data = await src.read(READ_SIZE)
                if not data:
                    break
                last_activity = loop.time()
                await dst.write(data)
                counts[index] += len(data)
        except (OSError, ConnectionError):
            pass
        finally:
            with contextlib.suppress(OSError, ConnectionError):
                dst.write_eof()

    async def watchdog(pipes: asyncio.Future[Any]) -> None:
        while not pipes.done():
            remaining = last_activity + idle_timeout - loop.time()
            if remaining <= 0:
                pipes.cancel()
                return
            await asyncio.sleep(remaining)

    pipes = asyncio.gather(pipe(a, b, 0), pipe(b, a, 1))
    dog = asyncio.create_task(watchdog(pipes))
    try:
        try:
            await pipes
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise  # cancelled from outside (shutdown), not by the idle watchdog
    finally:
        dog.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await dog
        await asyncio.gather(a.close(), b.close(), return_exceptions=True)
    return counts[0], counts[1]
