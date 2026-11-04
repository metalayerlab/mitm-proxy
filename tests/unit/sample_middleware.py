"""Middleware for load_middleware tests (importable by module path)."""

from collections.abc import Mapping
from typing import Any

from mitm_proxy.http.flow import Flow
from mitm_proxy.pipeline import Action, Middleware


class Sample(Middleware):
    pass


class Broken(Middleware):
    def __init__(self) -> None:
        raise RuntimeError("cannot construct")


instance = Sample()
not_middleware = object()


class SyncHook(Middleware):
    """A hook that isn't an async def: refused at startup and on reload (0011)."""

    def on_request(self, flow: Flow) -> Action:  # type: ignore[override]  # the mistake under test
        return Action.CONTINUE


class WithOptions(Middleware):
    """Takes its middleware.config entry (design 0012), and validates it."""

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        self.options = options
        if options is not None and "bad" in options:
            raise ValueError("'bad' is not an option")
