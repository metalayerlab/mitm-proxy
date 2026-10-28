"""Middleware for load_middleware tests (importable by module path)."""

from mitm_proxy.pipeline import Middleware


class Sample(Middleware):
    pass


class Broken(Middleware):
    def __init__(self) -> None:
        raise RuntimeError("cannot construct")


instance = Sample()
not_middleware = object()
