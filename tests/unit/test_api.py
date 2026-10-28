from typing import Any

import pytest
from fastapi.testclient import TestClient

from mitm_proxy.api import create_app
from mitm_proxy.config import Config, load_config
from mitm_proxy.metrics import Metrics
from mitm_proxy.tls.ca import CertificateAuthority
from tests.helpers import make_ca


class FakeRuntime:
    def __init__(self) -> None:
        self.state = "ready"
        self.metrics = Metrics("9.9.9")
        self.ca: CertificateAuthority = make_ca("api test CA")

    def status(self) -> dict[str, Any]:
        return {"state": self.state, "mode": "standalone"}


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime()


@pytest.fixture
def client(tmp_path: Any, runtime: FakeRuntime) -> TestClient:
    config: Config = load_config(tmp_path / "missing.yaml", environ={})
    return TestClient(create_app(config, runtime))


def test_health(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize(("state", "code"), [("ready", 200), ("starting", 503), ("stopping", 503)])
def test_ready_follows_the_proxy(
    client: TestClient, runtime: FakeRuntime, state: str, code: int
) -> None:
    runtime.state = state
    response = client.get("/readyz")
    assert (response.status_code, response.json()) == (code, {"status": state})


def test_status(client: TestClient) -> None:
    assert client.get("/v1/status").json() == {"state": "ready", "mode": "standalone"}


def test_metrics_are_prometheus_text(client: TestClient) -> None:
    response = client.get("/metrics")
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert 'mitm_proxy_build_info{version="9.9.9"} 1' in response.text


def test_ca_downloads(client: TestClient, runtime: FakeRuntime) -> None:
    pem = client.get("/ca.pem")
    assert pem.headers["content-type"] == "application/x-pem-file"
    assert pem.content == runtime.ca.cert_pem
    der = client.get("/ca.der")
    assert der.headers["content-type"] == "application/x-x509-ca-cert"
    assert der.content == runtime.ca.cert_der


def test_removed_routes_are_gone(client: TestClient) -> None:
    assert client.get("/version").status_code == 404
    assert client.get("/stats").status_code == 404


def test_without_a_runtime_everything_but_health_is_503(tmp_path: Any) -> None:
    client = TestClient(create_app(load_config(tmp_path / "missing.yaml", environ={})))
    assert client.get("/healthz").status_code == 200
    for path in ("/readyz", "/v1/status", "/metrics", "/ca.pem", "/ca.der"):
        assert client.get(path).status_code == 503, path
