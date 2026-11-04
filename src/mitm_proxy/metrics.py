"""The proxy's Prometheus metrics, for ``GET /metrics``.

The metric types and the text format come from interpose (design 0009); this module says which
metrics exist, their help, buckets and labels. Every label takes its values from a set declared
here, up front, so the intercepted host can never become a label (it would make the series count
unbounded): a value outside the set is a ``ValueError`` at the call that used it. The sets that
grow are ``middleware`` and capture's ``rule``, declared from the configuration when a snapshot is
built (``ProxyServer._build``), never from traffic.
"""

from __future__ import annotations

from collections.abc import Iterable

from interpose.metrics import Counter, Gauge, Histogram, Label, Metric, Registry

from mitm_proxy.classify import Decision
from mitm_proxy.http.flow import OUTCOME_KINDS, PLAIN_HTTP, Flow, FlowType

# Network-scale times (Prometheus's defaults, plus 30 s and 60 s), and middleware hook times
# (100 µs up to the default hook timeout). Fixed: changing them breaks dashboards (0007).
NETWORK_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)
MIDDLEWARE_BUCKETS = (
    0.0001,
    0.00025,
    0.0005,
    0.001,
    0.0025,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1,
    2.5,
    5,
)

# The values each fixed label may take.
FLOW_TYPES = (*(t.value for t in FlowType), PLAIN_HTTP)
TUNNEL_REASONS = tuple(d.value for d in Decision)
DIRECTIONS = ("request", "response")
UPSTREAM_PROTOCOLS = ("http/1.1", "h2")  # what ConnectionPool.counts() reports
CONFIG_RESULTS = ("applied",)
CERTIFICATE_KINDS = ("ca",)
REUSED = ("true", "false")
CAPTURE_RESULTS = ("written", "failed")


class Metrics:
    """Every metric the proxy exports, in the order /metrics lists them."""

    def __init__(self, version: str) -> None:
        # Shared by the failures counter and the duration histogram: one declare() covers both.
        self.middleware = Label("middleware")
        reg = self.registry = Registry()
        self.build_info = reg.register(
            Gauge(
                "mitm_proxy_build_info",
                "Always 1; the version label.",
                (Label("version", (version,)),),
            )
        )
        self.build_info.set(1, version)
        self.client_connections = reg.register(
            Gauge("mitm_proxy_client_connections", "Client connections open now.")
        )
        self.client_connections.set(0)
        self.flows = reg.register(
            Counter(
                "mitm_proxy_flows_total",
                "Flows handled, by type: http (intercepted HTTPS), plain_http, websocket.",
                (Label("type", FLOW_TYPES),),
            )
        )
        self.tunnels = reg.register(
            Counter(
                "mitm_proxy_tunnels_total",
                "CONNECT tunnels relayed without interception, by reason.",
                (Label("reason", TUNNEL_REASONS),),
            )
        )
        self.middleware_failures = reg.register(
            Counter(
                "mitm_proxy_middleware_failures_total",
                "Middleware hooks that raised or timed out, by middleware.",
                (self.middleware,),
            )
        )
        self.oversize_passthrough = reg.register(
            Counter(
                "mitm_proxy_oversize_passthrough_total",
                "Bodies over body.max_buffer_bytes, streamed through without middleware.",
                (Label("direction", DIRECTIONS),),
            )
        )
        self.proxy_auth_failures = reg.register(
            Counter(
                "mitm_proxy_proxy_auth_failures_total",
                "Requests refused with 407 (missing or wrong proxy credentials).",
            )
        )
        self.upstream_connections = reg.register(
            Gauge(
                "mitm_proxy_upstream_connections",
                "Pooled upstream connections, by protocol.",
                (Label("protocol", UPSTREAM_PROTOCOLS),),
            )
        )
        self.config_applies = reg.register(
            Counter(
                "mitm_proxy_config_applies_total",
                "Configurations applied, by result.",
                (Label("result", CONFIG_RESULTS),),
            )
        )
        self.certificate_expiry = reg.register(
            Gauge(
                "mitm_proxy_certificate_expiry_timestamp_seconds",
                "When a certificate expires (Unix time), by kind.",
                (Label("kind", CERTIFICATE_KINDS),),
            )
        )
        # Intercepted HTTP flows, observed when each ends (0007).
        self.flow_duration = reg.register(
            Histogram(
                "mitm_proxy_flow_duration_seconds",
                "Intercepted HTTP flows, from the request arriving to the last byte sent, "
                "by outcome.",
                NETWORK_BUCKETS,
                (Label("outcome", OUTCOME_KINDS),),
            )
        )
        self.upstream_connect = reg.register(
            Histogram(
                "mitm_proxy_upstream_connect_seconds",
                "Waiting for an upstream connection, by whether a pooled one was reused.",
                NETWORK_BUCKETS,
                (Label("reused", REUSED),),
            )
        )
        self.origin_ttfb = reg.register(
            Histogram(
                "mitm_proxy_origin_ttfb_seconds",
                "The origin's share: from the request being sent to its response headers.",
                NETWORK_BUCKETS,
            )
        )
        self.middleware_duration = reg.register(
            Histogram(
                "mitm_proxy_middleware_duration_seconds",
                "Time each middleware spent in its hooks for one flow, on_done included.",
                MIDDLEWARE_BUCKETS,
                (self.middleware,),
            )
        )
        # The capture middleware's records (design 0012), last, so the text before it is
        # unchanged. Its `rule` values come from the configuration, at build and reload; the
        # oversize counter's `direction` above is a body's (request/response), not a flow's.
        self.capture_rule = Label("rule")
        self.capture_records = reg.register(
            Counter(
                "mitm_proxy_capture_records_total",
                "Records the capture middleware wrote or dropped, by rule and result.",
                (self.capture_rule, Label("result", CAPTURE_RESULTS)),
            )
        )

    def declare_middleware(self, names: Iterable[str]) -> None:
        """Allow these middleware names as label values (the registry's, at build and reload)."""
        self.middleware.declare(*names)

    def declare_capture_rules(self, names: Iterable[str]) -> None:
        """Allow these capture rule names as label values (from the configuration, 0009)."""
        self.capture_rule.declare(*names)

    def all(self) -> list[Metric]:
        return list(self.registry)

    def observe_flow(self, flow: Flow) -> None:
        """The histograms' share of one ended, intercepted HTTP flow (0007)."""
        t, outcome = flow.timings, flow.outcome
        if t.total is not None and outcome is not None:
            self.flow_duration.observe(t.total, outcome.kind)
        if t.connect is not None:
            self.upstream_connect.observe(t.connect, "true" if t.connection_reused else "false")
        if t.ttfb is not None:
            self.origin_ttfb.observe(t.ttfb)
        for name, seconds in t.middleware.items():
            self.middleware_duration.observe(seconds, name)

    def render(self) -> str:
        return self.registry.render()
