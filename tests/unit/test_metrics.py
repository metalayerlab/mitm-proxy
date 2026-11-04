from pathlib import Path

import pytest
from interpose.metrics import CONTENT_TYPE, Gauge, Histogram, Label

from mitm_proxy.classify import Decision
from mitm_proxy.http.flow import OUTCOME_KINDS, FlowType
from mitm_proxy.metrics import (
    FLOW_TYPES,
    MIDDLEWARE_BUCKETS,
    NETWORK_BUCKETS,
    TUNNEL_REASONS,
    Metrics,
)

GOLDEN = Path(__file__).with_name("metrics_golden.txt")


def test_render_is_prometheus_text() -> None:
    m = Metrics("1.2.3")
    m.flows.inc("http")
    m.flows.inc("http")
    odd = 'we"ird\\name\n'
    m.declare_middleware([odd])  # every label value is declared before it is used (0009)
    m.middleware_failures.inc(odd)
    m.certificate_expiry.set(1.5, "ca")
    text = m.render()
    assert "# TYPE mitm_proxy_flows_total counter" in text
    assert 'mitm_proxy_flows_total{type="http"} 2' in text
    assert 'mitm_proxy_build_info{version="1.2.3"} 1' in text
    assert 'middleware="we\\"ird\\\\name\\n"' in text
    assert 'mitm_proxy_certificate_expiry_timestamp_seconds{kind="ca"} 1.5' in text
    assert text.endswith("\n")


def _populated() -> Metrics:
    """Every metric with samples, as the hand-written exposition rendered them before 0009."""
    m = Metrics("1.2.3")
    m.declare_middleware(["audit", "block"])
    m.client_connections.add(2)
    m.client_connections.add(-1)
    for kind in ("http", "http", "plain_http", "websocket"):
        m.flows.inc(kind)
    for reason in ("out_of_scope", "not_tls", "sni_out_of_scope"):
        m.tunnels.inc(reason)
    m.middleware_failures.inc("audit")
    m.middleware_failures.inc("block")
    m.oversize_passthrough.inc("request")
    m.oversize_passthrough.inc("response")
    m.proxy_auth_failures.inc()
    m.upstream_connections.read = lambda: {("http/1.1",): 1.0, ("h2",): 3.0}
    m.config_applies.inc("applied")
    m.certificate_expiry.set(1798761600.5, "ca")
    for value, outcome in (
        (0.003, "origin"),
        (0.2, "origin"),
        (75.0, "origin"),
        (0.05, "denied"),
        (0.01, "local"),
        (1.5, "error"),
        (2, "aborted"),
    ):
        m.flow_duration.observe(value, outcome)
    m.upstream_connect.observe(0.02, "false")
    m.upstream_connect.observe(0.0004, "true")
    m.origin_ttfb.observe(0.125)
    m.middleware_duration.observe(0.00005, "audit")
    m.middleware_duration.observe(0.3, "audit")
    m.middleware_duration.observe(6.0, "block")
    m.declare_capture_rules(["inbound-api"])
    m.capture_records.inc("inbound-api", "written")
    m.capture_records.inc("inbound-api", "written")
    m.capture_records.inc("inbound-api", "failed")
    return m


def test_output_is_byte_for_byte_what_it_was() -> None:
    # metrics_golden.txt was rendered by mitm-proxy's own exposition (v0.2.0) from the same
    # calls: dashboards and recording rules built on 0007 depend on this text (0009). The
    # capture metric (design 0012) was appended after it; the text before it is unchanged.
    assert _populated().render() == GOLDEN.read_text()


def test_the_content_type_is_unchanged() -> None:
    assert CONTENT_TYPE == "text/plain; version=0.0.4; charset=utf-8"


def test_every_label_value_the_proxy_uses_is_declared() -> None:
    assert set(FLOW_TYPES) == {t.value for t in FlowType} | {"plain_http"}
    assert set(TUNNEL_REASONS) == {d.value for d in Decision}
    m = Metrics("1")
    for outcome in OUTCOME_KINDS:
        m.flow_duration.observe(0.1, outcome)
    assert len(m.flow_duration.series) == len(OUTCOME_KINDS)


def test_an_undeclared_value_is_refused_without_naming_it() -> None:
    m = Metrics("1")
    with pytest.raises(ValueError, match="'middleware'") as e:
        m.middleware_failures.inc("example.org")
    assert "example.org" not in str(e.value)
    with pytest.raises(ValueError):
        m.flows.inc("example.org")
    assert "example.org" not in m.render()


def test_one_declare_covers_both_middleware_metrics() -> None:
    m = Metrics("1")
    m.declare_middleware(["audit"])
    m.middleware_failures.inc("audit")
    m.middleware_duration.observe(0.01, "audit")
    assert 'mitm_proxy_middleware_failures_total{middleware="audit"} 1' in m.render()
    assert m.middleware_duration.series.keys() == {("audit",)}


def test_a_reader_reporting_an_undeclared_protocol_fails_the_render() -> None:
    m = Metrics("1")
    m.upstream_connections.read = lambda: {("h3",): 1.0}
    with pytest.raises(ValueError, match="'protocol'"):
        m.render()


def test_gauge_with_a_reader() -> None:
    g = Gauge("g", "help", (Label("protocol", ("h2",)),), read=lambda: {("h2",): 3.0})
    assert g.render()[-1] == 'g{protocol="h2"} 3'
    plain = Gauge("p", "help")
    plain.add(2)
    plain.add(-1)
    assert plain.render()[-1] == "p 1"


def test_histogram_renders_cumulative_buckets_sum_and_count() -> None:
    h = Histogram("h_seconds", "help", (0.1, 1), (Label("outcome", ("origin",)),))
    for value in (0.05, 0.1, 0.5, 7):  # 0.1 lands in le="0.1": le is inclusive
        h.observe(value, "origin")
    assert h.render() == [
        "# HELP h_seconds help",
        "# TYPE h_seconds histogram",
        'h_seconds_bucket{outcome="origin",le="0.1"} 2',
        'h_seconds_bucket{outcome="origin",le="1.0"} 3',
        'h_seconds_bucket{outcome="origin",le="+Inf"} 4',
        'h_seconds_sum{outcome="origin"} 7.65',
        'h_seconds_count{outcome="origin"} 4',
    ]


def test_histogram_without_labels_or_observations() -> None:
    h = Histogram("t", "help", (1,))
    assert h.render() == ["# HELP t help", "# TYPE t histogram"]
    h.observe(2)
    expected = ['t_bucket{le="1.0"} 0', 't_bucket{le="+Inf"} 1', "t_sum 2", "t_count 1"]
    assert h.render()[2:] == expected


def test_bucket_sets_match_the_design() -> None:
    assert NETWORK_BUCKETS[0] == 0.005 and NETWORK_BUCKETS[-1] == 60
    assert MIDDLEWARE_BUCKETS[0] == 0.0001 and MIDDLEWARE_BUCKETS[-1] == 5
    assert len(NETWORK_BUCKETS) == 13 and len(MIDDLEWARE_BUCKETS) == 15


@pytest.mark.parametrize("name", ["flow_duration", "upstream_connect", "origin_ttfb"])
def test_the_flow_histograms_are_exported(name: str) -> None:
    m = Metrics("1")
    assert getattr(m, name) in m.all()
    assert "# TYPE mitm_proxy_middleware_duration_seconds histogram" in m.render()
