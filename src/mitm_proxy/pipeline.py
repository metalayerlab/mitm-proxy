"""The middleware API and the pipeline runner.

Middleware runs in registration order (a plain list: no priorities). Each hook is
awaited in turn, inside the read/write loop, and never scheduled as a background
task: that sequencing is what carries backpressure from a slow hook or a slow
peer back to the reader. Do not introduce ``create_task`` here.

A hook that raises or exceeds the hook timeout is logged with the middleware's
name and the flow ID, and that middleware is skipped for the rest of the flow,
as if it had returned CONTINUE / None. Nothing a middleware does can crash the
connection or the process. (A hook that blocks the event loop can't be timed
out; that is the one thing middleware must not do.)
"""

from __future__ import annotations

import asyncio
import enum
import importlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from mitm_proxy.errors import StartupError
from mitm_proxy.http.flow import Flow

log = logging.getLogger(__name__)


class Action(enum.Enum):
    CONTINUE = "continue"  # on to the next middleware
    STOP = "stop"  # end this phase; the flow stands as it is (set flow.response to answer)
    DENY = "deny"  # reject the flow


class Middleware:
    """Base class. Override what you need; the defaults do nothing.

    Overriding a chunk hook makes the middleware *mutation-capable*: flows it
    applies to are sent with length-independent framing.
    """

    def applies_to(self, flow: Flow) -> bool:
        """Called once, when the request headers arrive. False: no hooks run for this flow."""
        return True

    async def on_request(self, flow: Flow) -> Action:
        return Action.CONTINUE

    async def on_request_chunk(self, flow: Flow, chunk: bytes, is_last: bool) -> bytes | None:
        """None passes the chunk on unchanged; bytes replace it."""
        return None

    async def on_response(self, flow: Flow) -> Action:
        return Action.CONTINUE

    async def on_response_chunk(self, flow: Flow, chunk: bytes, is_last: bool) -> bytes | None:
        """None passes the chunk on unchanged; bytes replace it."""
        return None


def is_mutation_capable(middleware: Middleware) -> bool:
    cls = type(middleware)
    return (
        cls.on_request_chunk is not Middleware.on_request_chunk
        or cls.on_response_chunk is not Middleware.on_response_chunk
    )


@dataclass(frozen=True)
class Entry:
    name: str
    middleware: Middleware
    mutation_capable: bool


@dataclass(frozen=True)
class Outcome:
    action: Action
    by: str | None = None  # the middleware that stopped or denied


class Pipeline:
    def __init__(
        self, middleware: Sequence[tuple[str, Middleware]] = (), *, hook_timeout: float = 5.0
    ) -> None:
        self.hook_timeout = hook_timeout
        self._entries: list[Entry] = []
        for name, mw in middleware:
            self.register(name, mw)

    def register(self, name: str, middleware: Middleware) -> None:
        if not name:
            raise ValueError("a middleware needs a name")
        if any(e.name == name for e in self._entries):
            raise ValueError(f"middleware {name!r} is registered twice")
        if not isinstance(middleware, Middleware):
            raise TypeError(f"middleware {name!r} is not a Middleware")
        self._entries.append(Entry(name, middleware, is_mutation_capable(middleware)))

    @property
    def names(self) -> list[str]:
        return [e.name for e in self._entries]

    def start(self, flow: Flow) -> FlowPipeline:
        """The middleware that applies to flow, decided once, now."""
        run = FlowPipeline(flow, self.hook_timeout)
        for entry in self._entries:
            try:
                applies = entry.middleware.applies_to(flow)
            except Exception:
                run.fail(entry.name, "applies_to")
                continue
            if applies:
                run.entries.append(entry)
        return run


class FlowPipeline:
    """The pipeline for one flow: what applies, and what has failed so far."""

    def __init__(self, flow: Flow, hook_timeout: float) -> None:
        self.flow = flow
        self.hook_timeout = hook_timeout
        self.entries: list[Entry] = []
        self.failed: set[str] = set()
        self.request_skipped = False
        self.response_skipped = False
        # Called with a middleware's name when it fails (metrics).
        self.on_failure: Callable[[str], None] | None = None

    @property
    def applies(self) -> bool:
        return bool(self.entries)

    @property
    def mutation_capable(self) -> bool:
        return any(e.mutation_capable for e in self.entries)

    def fail(self, name: str, hook: str) -> None:
        """Record that name raised in hook (call from an except block: logs the traceback)."""
        log.exception("middleware %r failed in %s (flow %s)", name, hook, self.flow.id)
        self._mark(name)

    def _mark(self, name: str) -> None:
        self.failed.add(name)
        if self.on_failure is not None:
            self.on_failure(name)

    def _active(self) -> list[Entry]:
        return [e for e in self.entries if e.name not in self.failed]

    async def _call(self, entry: Entry, hook: str, *args: Any) -> Any:
        """Await one hook under the timeout; on failure, log it and return None."""
        method: Callable[..., Awaitable[Any]] = getattr(entry.middleware, hook)
        start = time.monotonic()
        try:
            async with asyncio.timeout(self.hook_timeout):
                return await method(*args)
        except TimeoutError:
            log.error(
                "middleware %r timed out in %s after %.1fs (flow %s)",
                entry.name,
                hook,
                self.hook_timeout,
                self.flow.id,
            )
            self._mark(entry.name)
        except Exception:
            self.fail(entry.name, hook)
        finally:
            spent = self.flow.timings.middleware
            spent[entry.name] = spent.get(entry.name, 0.0) + time.monotonic() - start
        return None

    async def _run_phase(self, hook: str) -> Outcome:
        for entry in self._active():
            result = await self._call(entry, hook, self.flow)
            if entry.name in self.failed or result is None or result is Action.CONTINUE:
                continue
            if not isinstance(result, Action):
                log.error(
                    "middleware %r returned %r from %s, not an Action (flow %s)",
                    entry.name,
                    result,
                    hook,
                    self.flow.id,
                )
                self._mark(entry.name)
                continue
            return Outcome(result, entry.name)
        return Outcome(Action.CONTINUE)

    async def _run_chunk(self, hook: str, chunk: bytes, is_last: bool) -> bytes:
        for entry in self._active():
            result = await self._call(entry, hook, self.flow, chunk, is_last)
            if entry.name in self.failed or result is None:
                continue
            if not isinstance(result, bytes | bytearray | memoryview):
                log.error(
                    "middleware %r returned %r from %s, not bytes or None (flow %s)",
                    entry.name,
                    type(result).__name__,
                    hook,
                    self.flow.id,
                )
                self._mark(entry.name)
                continue
            chunk = bytes(result)
        return chunk

    async def run_request(self) -> Outcome:
        if self.request_skipped:
            return Outcome(Action.CONTINUE)
        return await self._run_phase("on_request")

    async def run_request_chunk(self, chunk: bytes, is_last: bool) -> bytes:
        if self.request_skipped:
            return chunk
        return await self._run_chunk("on_request_chunk", chunk, is_last)

    async def run_response(self) -> Outcome:
        if self.response_skipped:
            return Outcome(Action.CONTINUE)
        return await self._run_phase("on_response")

    async def run_response_chunk(self, chunk: bytes, is_last: bool) -> bytes:
        if self.response_skipped:
            return chunk
        return await self._run_chunk("on_response_chunk", chunk, is_last)


def load_middleware(
    available: Mapping[str, str], chain: Sequence[str]
) -> list[tuple[str, Middleware]]:
    """Import the chain's middleware from the registry (name -> "module:attribute")."""
    loaded: list[tuple[str, Middleware]] = []
    for name in chain:
        ref = available[name]
        module_name, _, attribute = ref.partition(":")
        try:
            obj: Any = importlib.import_module(module_name)
            for part in attribute.split("."):
                obj = getattr(obj, part)
        except Exception as e:
            raise StartupError(f"middleware {name!r}: cannot load {ref}: {e}") from e
        if isinstance(obj, type) and issubclass(obj, Middleware):
            try:
                obj = obj()
            except Exception as e:
                raise StartupError(f"middleware {name!r}: {ref}() failed: {e}") from e
        if not isinstance(obj, Middleware):
            raise StartupError(f"middleware {name!r}: {ref} is not a Middleware")
        loaded.append((name, obj))
    return loaded
