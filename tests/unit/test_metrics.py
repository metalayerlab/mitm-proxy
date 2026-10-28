import pytest

from mitm_proxy.metrics import MIDDLEWARE_BUCKETS, NETWORK_BUCKETS, Gauge, Histogram, Metrics


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


def test_histogram_renders_cumulative_buckets_sum_and_count() -> None:
    h = Histogram("h_seconds", "help", (0.1, 1), ("outcome",))
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
