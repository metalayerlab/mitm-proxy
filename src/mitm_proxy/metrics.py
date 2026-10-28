"""Prometheus text exposition, for ``GET /metrics``.

Counters, gauges and classic histograms: few enough that the format is written
here rather than taking a dependency. Every label has a small, fixed set of
values: the intercepted host is never a label (it would make the series count
unbounded).
"""

from __future__ import annotations

import bisect
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mitm_proxy.http.flow import Flow

LabelValues = tuple[str, ...]


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _format(name: str, labels: tuple[str, ...], values: LabelValues, value: float) -> str:
    if labels:
        pairs = ",".join(f'{k}="{_escape(v)}"' for k, v in zip(labels, values, strict=True))
        name = f"{name}{{{pairs}}}"
    number = repr(float(value)) if value != int(value) else str(int(value))
    return f"{name} {number}"


class Metric:
    def __init__(self, name: str, help: str, kind: str, labels: tuple[str, ...] = ()) -> None:
        self.name = name
        self.help = help
        self.kind = kind
        self.labels = labels
        self.values: dict[LabelValues, float] = {}

    def samples(self) -> Mapping[LabelValues, float]:
        return self.values

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {self.kind}"]
        for values, value in sorted(self.samples().items()):
            lines.append(_format(self.name, self.labels, values, value))
        return lines


class Counter(Metric):
    def __init__(self, name: str, help: str, labels: tuple[str, ...] = ()) -> None:
        super().__init__(name, help, "counter", labels)

    def inc(self, *values: str, amount: float = 1.0) -> None:
        self.values[values] = self.values.get(values, 0.0) + amount


class Gauge(Metric):
    def __init__(
        self,
        name: str,
        help: str,
        labels: tuple[str, ...] = (),
        read: Callable[[], Mapping[LabelValues, float]] | None = None,
    ) -> None:
        super().__init__(name, help, "gauge", labels)
        self.read = read  # if set, the samples come from here, not from set()/add()

    def set(self, value: float, *values: str) -> None:
        self.values[values] = value

    def add(self, amount: float, *values: str) -> None:
        self.values[values] = self.values.get(values, 0.0) + amount

    def samples(self) -> Mapping[LabelValues, float]:
        return self.read() if self.read is not None else self.values


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


def _le(bound: float) -> str:
    return repr(float(bound))


class Histogram(Metric):
    """A classic histogram: cumulative buckets, a sum and a count, per label set."""

    def __init__(
        self, name: str, help: str, buckets: tuple[float, ...], labels: tuple[str, ...] = ()
    ) -> None:
        super().__init__(name, help, "histogram", labels)
        self.buckets = tuple(sorted(buckets))
        # per label set: counts per bucket (the last is +Inf, not cumulative), sum, count
        self.series: dict[LabelValues, tuple[list[int], list[float]]] = {}

    def observe(self, value: float, *values: str) -> None:
        counts, totals = self.series.setdefault(values, ([0] * (len(self.buckets) + 1), [0.0]))
        counts[bisect.bisect_left(self.buckets, value)] += 1  # le is inclusive
        totals[0] += value

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {self.kind}"]
        labels = (*self.labels, "le")
        for values, (counts, totals) in sorted(self.series.items()):
            running = 0
            bounds = [*map(_le, self.buckets), "+Inf"]
            for bound, count in zip(bounds, counts, strict=True):
                running += count
                lines.append(_format(f"{self.name}_bucket", labels, (*values, bound), running))
            lines.append(_format(f"{self.name}_sum", self.labels, values, totals[0]))
            lines.append(_format(f"{self.name}_count", self.labels, values, running))
        return lines


class Metrics:
    """Every metric the proxy exports."""

    def __init__(self, version: str) -> None:
        self.build_info = Gauge(
            "mitm_proxy_build_info", "Always 1; the version label.", ("version",)
        )
        self.build_info.set(1, version)
        self.client_connections = Gauge(
            "mitm_proxy_client_connections", "Client connections open now."
        )
        self.client_connections.set(0)
        self.flows = Counter(
            "mitm_proxy_flows_total",
            "Flows handled, by type: http (intercepted HTTPS), plain_http, websocket.",
            ("type",),
        )
        self.tunnels = Counter(
            "mitm_proxy_tunnels_total",
            "CONNECT tunnels relayed without interception, by reason.",
            ("reason",),
        )
        self.middleware_failures = Counter(
            "mitm_proxy_middleware_failures_total",
            "Middleware hooks that raised or timed out, by middleware.",
            ("middleware",),
        )
        self.oversize_passthrough = Counter(
            "mitm_proxy_oversize_passthrough_total",
            "Bodies over body.max_buffer_bytes, streamed through without middleware.",
            ("direction",),
        )
        self.proxy_auth_failures = Counter(
            "mitm_proxy_proxy_auth_failures_total",
            "Requests refused with 407 (missing or wrong proxy credentials).",
        )
        self.upstream_connections = Gauge(
            "mitm_proxy_upstream_connections",
            "Pooled upstream connections, by protocol.",
            ("protocol",),
        )
        self.config_applies = Counter(
            "mitm_proxy_config_applies_total",
            "Configurations applied, by result.",
            ("result",),
        )
        self.certificate_expiry = Gauge(
            "mitm_proxy_certificate_expiry_timestamp_seconds",
            "When a certificate expires (Unix time), by kind.",
            ("kind",),
        )
        # Intercepted HTTP flows, observed when each ends (0007).
        self.flow_duration = Histogram(
            "mitm_proxy_flow_duration_seconds",
            "Intercepted HTTP flows, from the request arriving to the last byte sent, by outcome.",
            NETWORK_BUCKETS,
            ("outcome",),
        )
        self.upstream_connect = Histogram(
            "mitm_proxy_upstream_connect_seconds",
            "Waiting for an upstream connection, by whether a pooled one was reused.",
            NETWORK_BUCKETS,
            ("reused",),
        )
        self.origin_ttfb = Histogram(
            "mitm_proxy_origin_ttfb_seconds",
            "The origin's share: from the request being sent to its response headers.",
            NETWORK_BUCKETS,
        )
        self.middleware_duration = Histogram(
            "mitm_proxy_middleware_duration_seconds",
            "Time each middleware spent in its hooks for one flow, on_done included.",
            MIDDLEWARE_BUCKETS,
            ("middleware",),
        )

    def all(self) -> list[Metric]:
        return [
            self.build_info,
            self.client_connections,
            self.flows,
            self.tunnels,
            self.middleware_failures,
            self.oversize_passthrough,
            self.proxy_auth_failures,
            self.upstream_connections,
            self.config_applies,
            self.certificate_expiry,
            self.flow_duration,
            self.upstream_connect,
            self.origin_ttfb,
            self.middleware_duration,
        ]

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
        lines: list[str] = []
        for metric in self.all():
            lines.extend(metric.render())
        return "\n".join(lines) + "\n"
