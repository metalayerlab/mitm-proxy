"""The HTTP API.

The wiki's HTTP-API-Reference endpoint table is generated from this app's OpenAPI
schema (scripts/gen-wiki.py): give every route a `summary`, and a docstring when
it needs more than a line.
"""

from __future__ import annotations

from fastapi import FastAPI

from mitm_proxy import __version__
from mitm_proxy.config import Config


def create_app(config: Config) -> FastAPI:
    app = FastAPI(
        title="mitm-proxy",
        version=__version__,
        description="Async Python MITM proxy with a self-signed CA — HTTP/1.1 & HTTP/2, pluggable middleware for inspecting and rewriting traffic in flight.",
    )
    app.state.config = config

    @app.get("/healthz", summary="Liveness: the process is up", tags=["health"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", summary="Readiness: able to serve requests", tags=["health"])
    def readyz() -> dict[str, str]:
        # Check what the service depends on (database, upstream API) here, and
        # answer 503 when it can't serve. Liveness must stay dependency-free.
        return {"status": "ready"}

    @app.get("/version", summary="The running version", tags=["meta"])
    def get_version() -> dict[str, str]:
        return {"version": __version__}

    return app
