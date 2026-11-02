"""The middleware API, and mitm-proxy's pipeline on interpose's core (design 0010).

Middleware runs in registration order (a plain list: no priorities). Each hook is
awaited in turn, inside the read/write loop, and never scheduled as a background
task: that sequencing is what carries backpressure from a slow hook or a slow
peer back to the reader. interpose's ``Run`` keeps that rule; do not introduce
``create_task`` here.

A hook that raises, returns the wrong type or exceeds the hook timeout is logged
with the middleware's name and the flow ID, and that middleware is skipped for
the rest of the flow, as if it had returned CONTINUE / None: mitm-proxy's policy
(fail open, 0001). Nothing a middleware does can crash the connection or the
process. A middleware that sets ``on_error = "fail"`` instead denies the flow
when it fails (``HookFailed``; see ``FlowPipeline``). (A hook that blocks the
event loop can't be timed out; that is the one thing middleware must not do.)
"""

from __future__ import annotations

import enum
import importlib
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from interpose import pipeline as core
from interpose.pipeline import NO_LIMIT, HookFailed, InvalidResult, OnError

from mitm_proxy.errors import StartupError
from mitm_proxy.http.flow import Flow

__all__ = [
    "NO_LIMIT",
    "Action",
    "Entry",
    "FlowPipeline",
    "HookFailed",
    "Middleware",
    "Outcome",
    "Pipeline",
    "is_mutation_capable",
    "load_middleware",
    "policy",
]

log = logging.getLogger(__name__)

HOOK_TIMEOUT = 5.0  # seconds: middleware.hook_timeout_ms's default


class Action(enum.Enum):
    CONTINUE = "continue"  # on to the next middleware
    STOP = "stop"  # end this phase; the flow stands as it is (set flow.response to answer)
    DENY = "deny"  # reject the flow


class Middleware:
    """Base class. Override what you need; the defaults do nothing.

    Overriding a chunk hook makes the middleware *mutation-capable*: flows it
    applies to are sent with length-independent framing.

    Two class attributes set this middleware's own failure policy (interpose reads them):
    ``on_error`` (``"skip"``, the default: fail open; ``"fail"``: a failure denies the flow)
    and ``timeout`` (seconds per hook call instead of ``middleware.hook_timeout_ms``;
    ``NO_LIMIT`` for none). Don't use either name for anything else. The middleware's name is
    always its key in ``middleware.available``.
    """

    on_error: ClassVar[OnError | None] = None
    timeout: ClassVar[float | None] = None

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

    async def on_done(self, flow: Flow) -> None:
        """Called once, after the flow has ended, however it ended (flow.outcome). Observe only."""
        return None


def is_mutation_capable(middleware: Middleware) -> bool:
    cls = type(middleware)
    return (
        cls.on_request_chunk is not Middleware.on_request_chunk
        or cls.on_response_chunk is not Middleware.on_response_chunk
    )


def policy(hook_timeout: float = HOOK_TIMEOUT) -> core.Policy:
    """mitm-proxy's answers to interpose's questions: today's behaviour, written down (0010).

    Fail open; response hooks in registration order and never after a local answer (0005);
    on_done never reaches a middleware that failed (0006); the hook timeout from settings.
    """
    return core.Policy(
        on_error="skip",
        after_order="chain",
        after_local=False,
        observe_failed=False,
        timeout=hook_timeout,
    )


@dataclass(frozen=True)
class Entry:
    name: str
    middleware: Middleware
    mutation_capable: bool
    core: core.Entry[Middleware]  # the name and the middleware's own on_error and timeout


@dataclass(frozen=True)
class Outcome:
    action: Action
    by: str | None = None  # the middleware that stopped or denied


def _core_entry(name: str, middleware: Middleware) -> core.Entry[Middleware]:
    """interpose's entry: the registry name, and the middleware's on_error and timeout."""
    try:
        return core.Entry.of(middleware, name=name)
    except ValueError as e:
        raise ValueError(f"{e} (its on_error or timeout attribute)") from e


class Pipeline:
    def __init__(
        self,
        middleware: Sequence[tuple[str, Middleware]] = (),
        *,
        hook_timeout: float = HOOK_TIMEOUT,
    ) -> None:
        self.hook_timeout = hook_timeout
        self._entries: list[Entry] = []
        self._core = core.Pipeline[Middleware]([], policy(hook_timeout))
        for name, mw in middleware:
            self.register(name, mw)

    def register(self, name: str, middleware: Middleware) -> None:
        if not name:
            raise ValueError("a middleware needs a name")
        if any(e.name == name for e in self._entries):
            raise ValueError(f"middleware {name!r} is registered twice")
        if not isinstance(middleware, Middleware):
            raise TypeError(f"middleware {name!r} is not a Middleware")
        entry = Entry(
            name, middleware, is_mutation_capable(middleware), _core_entry(name, middleware)
        )
        self._entries.append(entry)
        # interpose's pipeline is frozen: build it again (registration happens at startup only).
        self._core = core.Pipeline([e.core for e in self._entries], self._core.policy)

    @property
    def names(self) -> list[str]:
        return [e.name for e in self._entries]

    def start(self, flow: Flow, on_failure: Callable[[str], None] | None = None) -> FlowPipeline:
        """The middleware that applies to flow, decided once, now.

        on_failure is called with a middleware's name when it fails, from applies_to on.
        """
        return FlowPipeline(flow, self, on_failure)


class FlowPipeline:
    """The pipeline for one flow: what applies, and what has failed so far.

    Thin wrappers over interpose's ``Run``. A middleware with ``on_error = "fail"`` that fails
    makes ``run_request``/``run_response`` return a DENY by it (403 and 502 in the exchange,
    as for a deliberate DENY), and makes a chunk hook raise ``HookFailed`` for the exchange to
    answer the same way. Under the default policy nothing raises.
    """

    def __init__(
        self,
        flow: Flow,
        pipeline: Pipeline,
        on_failure: Callable[[str], None] | None = None,
    ) -> None:
        self.flow = flow
        self.hook_timeout = pipeline.hook_timeout
        self.request_skipped = False
        self.response_skipped = False
        # Called with a middleware's name when it fails (metrics). Read at each failure.
        self.on_failure = on_failure
        # A fail-closed middleware's applies_to failed: the flow is denied by it (0010).
        self.denied_by: str | None = None
        by_core = {e.core: e for e in pipeline._entries}
        try:
            self._run = pipeline._core.start(
                select=lambda mw: mw.applies_to(flow),
                select_hook="applies_to",
                on_failure=self._failed,
                timings=flow.timings.middleware,
                state=flow.metadata,
            )
        except HookFailed as e:
            # Raised before any hook ran; nothing was selected, so no on_done either.
            self.denied_by = e.failure.middleware
            self._run = pipeline._core.start(select=lambda _: False, on_failure=self._failed)
        self._core_entries = pipeline._core.entries
        self.entries: list[Entry] = [by_core[e] for e in self._run.entries]

    def _failed(self, failure: core.Failure) -> None:
        """Log one failure with the flow ID, and count it."""
        name, hook, flow_id = failure.middleware, failure.hook, self.flow.id
        if failure.timed_out:
            log.error(
                "middleware %r timed out in %s after %.1fs (flow %s)",
                name,
                hook,
                failure.timeout,
                flow_id,
            )
        elif isinstance(failure.error, InvalidResult):
            # The type, never the value (interpose names only the type).
            log.error(
                "middleware %r: %s, not %s (flow %s)", name, failure.error, _EXPECTED[hook], flow_id
            )
        else:
            log.error(
                "middleware %r failed in %s (flow %s)", name, hook, flow_id, exc_info=failure.error
            )
        if self.on_failure is not None:
            self.on_failure(name)

    @property
    def applies(self) -> bool:
        return bool(self.entries) or self.denied_by is not None

    @property
    def mutation_capable(self) -> bool:
        return any(e.mutation_capable for e in self.entries)

    @property
    def failed(self) -> set[str]:
        """The names of the middleware that have failed in this flow, applies_to included."""
        failed = {e.name for e in self._core_entries if self._run.has_failed(e)}
        return failed | {self.denied_by} if self.denied_by is not None else failed

    async def _phase(self, hook: str, entries: Sequence[core.Entry[Middleware]] | None) -> Outcome:
        def invoke(mw: Middleware, flow: Flow) -> Any:
            return getattr(mw, hook)(flow)

        try:
            if entries is None:
                stopped = await self._run.before(
                    hook, self.flow, invoke, until=_stops, valid=_is_action
                )
            else:
                stopped = await self._run.each(
                    hook,
                    lambda mw: invoke(mw, self.flow),
                    entries=entries,
                    until=_stops,
                    valid=_is_action,
                )
        except HookFailed as e:
            return Outcome(Action.DENY, e.failure.middleware)
        if stopped is None:
            return Outcome(Action.CONTINUE)
        return Outcome(stopped.result, stopped.entry.name)

    async def _chunk(self, hook: str, chunk: bytes, is_last: bool) -> bytes:
        async def invoke(mw: Middleware, current: bytes) -> bytes | None:
            result = await getattr(mw, hook)(self.flow, current, is_last)
            if result is None:
                return None
            if not isinstance(result, bytes | bytearray | memoryview):
                raise InvalidResult(f"{hook} returned {type(result).__name__}")
            return bytes(result)  # every middleware gets bytes from the one before

        return await self._run.fold(hook, chunk, invoke, keep_none=True)

    async def run_request(self) -> Outcome:
        if self.denied_by is not None:
            return Outcome(Action.DENY, self.denied_by)
        if self.request_skipped:
            return Outcome(Action.CONTINUE)
        return await self._phase("on_request", None)

    async def run_request_chunk(self, chunk: bytes, is_last: bool) -> bytes:
        """Raises HookFailed when a fail-closed middleware fails."""
        if self.request_skipped:
            return chunk
        return await self._chunk("on_request_chunk", chunk, is_last)

    async def run_response(self) -> Outcome:
        if self.response_skipped:
            return Outcome(Action.CONTINUE)
        # What ran on_request and hasn't failed since; nothing after a local answer (0005).
        return await self._phase("on_response", [e for e, _ in self._run.after_pairs()])

    async def run_response_chunk(self, chunk: bytes, is_last: bool) -> bytes:
        """Raises HookFailed when a fail-closed middleware fails."""
        if self.response_skipped:
            return chunk
        return await self._chunk("on_response_chunk", chunk, is_last)

    async def run_done(self) -> None:
        """on_done for every middleware that applied and hasn't failed, in order.

        Not skipped for oversize bodies: every middleware that applied is told the flow ended.
        Always fails open, whatever a middleware's on_error says: the outcome is fixed.
        """
        await self._run.observe("on_done", lambda mw: mw.on_done(self.flow))


_EXPECTED = {
    "on_request": "an Action",
    "on_response": "an Action",
    "on_request_chunk": "bytes or None",
    "on_response_chunk": "bytes or None",
}


def _stops(result: Any) -> bool:
    return result is not None and result is not Action.CONTINUE


def _is_action(result: Any) -> bool:
    return result is None or isinstance(result, Action)


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
        try:
            _core_entry(name, obj)
        except ValueError as e:
            raise StartupError(str(e)) from e
        loaded.append((name, obj))
    return loaded
