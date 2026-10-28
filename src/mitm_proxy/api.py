"""The admin HTTP API: read-only. Health, status, metrics and the root CA download.

There are no endpoints that change state: management, when it comes, is an
outbound exchange with a control plane (design 0002). The wiki's
HTTP-API-Reference endpoint table is generated from this app's OpenAPI schema
(scripts/gen-wiki.py): give every route a `summary`, and a docstring when it
needs more than a line.
"""

from __future__ import annotations

from typing import Any, Protocol

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from mitm_proxy import DESCRIPTION, __version__
from mitm_proxy.config import Config
from mitm_proxy.metrics import Metrics
from mitm_proxy.tls.ca import CertificateAuthority

PROMETHEUS_TEXT = "text/plain; version=0.0.4; charset=utf-8"


class Runtime(Protocol):
    """What the API reads from the running proxy."""

    state: str  # "starting", "ready" or "stopping"
    metrics: Metrics
    ca: CertificateAuthority

    def status(self) -> dict[str, Any]: ...


def create_app(config: Config, runtime: Runtime | None = None) -> FastAPI:
    """The app. Without a runtime (e.g. to render the OpenAPI schema) it reports "starting"."""
    app = FastAPI(title="mitm-proxy", version=__version__, description=DESCRIPTION)
    app.state.config = config
    not_running = JSONResponse({"status": "starting"}, status_code=503)

    @app.get("/healthz", summary="Liveness: the process is up", tags=["health"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(
        "/readyz",
        summary="Readiness: the proxy listener is up and the CA is loaded",
        tags=["health"],
        responses={503: {"description": "Starting or stopping"}},
    )
    def readyz() -> JSONResponse:
        """200 `{"status": "ready"}`; 503 with `starting` or `stopping` otherwise."""
        state = runtime.state if runtime is not None else "starting"
        return JSONResponse({"status": state}, status_code=200 if state == "ready" else 503)

    @app.get("/v1/status", summary="Version, configuration, health and CA expiry", tags=["meta"])
    def status() -> Any:
        """No secrets and no hostnames: the configuration appears as a hash."""
        return runtime.status() if runtime is not None else not_running

    @app.get(
        "/metrics",
        summary="Prometheus metrics",
        tags=["meta"],
        response_class=PlainTextResponse,
    )
    def metrics() -> Response:
        if runtime is None:
            return not_running
        return Response(runtime.metrics.render(), media_type=PROMETHEUS_TEXT)

    @app.get(
        "/ca.pem",
        summary="The root CA certificate (PEM), to install on clients",
        tags=["ca"],
        response_class=Response,
    )
    def ca_pem() -> Response:
        if runtime is None:
            return not_running
        return Response(
            runtime.ca.cert_pem,
            media_type="application/x-pem-file",
            headers={"content-disposition": 'attachment; filename="mitm-proxy-ca.pem"'},
        )

    @app.get(
        "/ca.der",
        summary="The root CA certificate (DER), to install on clients",
        tags=["ca"],
        response_class=Response,
    )
    def ca_der() -> Response:
        """Served as `application/x-x509-ca-cert`, which phones offer to install."""
        if runtime is None:
            return not_running
        return Response(runtime.ca.cert_der, media_type="application/x-x509-ca-cert")

    return app
