from mitm_proxy.metrics import Gauge, Metrics


def test_render_is_prometheus_text() -> None:
    m = Metrics("1.2.3")
    m.flows.inc("http")
    m.flows.inc("http")
    m.middleware_failures.inc('we"ird\\name\n')
    m.certificate_expiry.set(1.5, "ca")
    text = m.render()
    assert "# TYPE mitm_proxy_flows_total counter" in text
    assert 'mitm_proxy_flows_total{type="http"} 2' in text
    assert 'mitm_proxy_build_info{version="1.2.3"} 1' in text
    assert 'middleware="we\\"ird\\\\name\\n"' in text
    assert 'mitm_proxy_certificate_expiry_timestamp_seconds{kind="ca"} 1.5' in text
    assert text.endswith("\n")


def test_gauge_with_a_reader() -> None:
    g = Gauge("g", "help", ("protocol",), read=lambda: {("h2",): 3.0})
    assert g.render()[-1] == 'g{protocol="h2"} 3'
    plain = Gauge("p", "help")
    plain.add(2)
    plain.add(-1)
    assert plain.render()[-1] == "p 1"
