"""Prometheus text exposition, for ``GET /metrics``.

Only counters and gauges, so the format is written here rather than taking a
dependency. Every label has a small, fixed set of values: the intercepted host is
never a label (it would make the series count unbounded).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

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
        self._read = read

    def set(self, value: float, *values: str) -> None:
        self.values[values] = value

    def add(self, amount: float, *values: str) -> None:
        self.values[values] = self.values.get(values, 0.0) + amount

    def samples(self) -> Mapping[LabelValues, float]:
        return self._read() if self._read is not None else self.values


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
        ]

    def render(self) -> str:
        lines: list[str] = []
        for metric in self.all():
            lines.extend(metric.render())
        return "\n".join(lines) + "\n"
