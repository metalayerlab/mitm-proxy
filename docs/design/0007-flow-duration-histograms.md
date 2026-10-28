# 0007: Flow duration histograms

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Implemented
Date: 2026-10-28
Authors: Stephen Cox; drafted by Claude (Claude Code)

## Context

Design 0003 put stage timings on every flow and left duration metrics for a later record. 0004 and 0005
put the same numbers in `Server-Timing` and the access log. Both are per flow: good for one request,
useless for "is the origin slower than yesterday?" or "what's our p99?". That needs the distribution
over time, in `/metrics`, where Prometheus (or anything that reads its text format) already scrapes
the proxy.

Constraints:

- **No dependency.** `/metrics` is hand-written (0001; 0002 kept it that way), and so far only counters
  and gauges are exported.
- **Bounded series.** Every label has a small, fixed set of values. The intercepted host is never a
  label (`metrics.py`).
- **Cost.** A few additions per flow, on the same event loop.
- **Aggregation.** The control plane (0002) will see a fleet, so whatever is exported must add up across
  instances.

## Decision

### Four histograms

Classic Prometheus histograms (cumulative `_bucket{le="…"}`, `_sum`, `_count`), in seconds, recorded for
every **intercepted HTTP flow** when it ends, in the same `finally` that writes the access log (0005).
They're always on, whatever `proxy.access_log` says.

| Metric | Labels | Observed | From `flow.timings` (0003) |
|---|---|---|---|
| `mitm_proxy_flow_duration_seconds` | `outcome` | every flow | `total` |
| `mitm_proxy_upstream_connect_seconds` | `reused` (`true`, `false`) | when a connection was leased | `connect` |
| `mitm_proxy_origin_ttfb_seconds` | none | when the origin answered | `ttfb` |
| `mitm_proxy_middleware_duration_seconds` | `middleware` | per middleware that ran, per flow | `middleware[name]` |

- **`outcome`** is the outcome's kind (0006): `origin`, `local`, `denied`, `error`, `aborted`. That keeps
  aborted downloads and instant local answers out of the origin flows' distribution. The middleware
  name isn't part of this label: it's already in the middleware histogram.
- **`reused`** separates the about-zero pooled leases from new TCP and TLS connections. Mixed together,
  they make a distribution that describes neither.
- **`middleware`** has the bounded set of names from `middleware.chain`, like
  `mitm_proxy_middleware_failures_total`. It holds each middleware's time for the whole flow, `on_done`
  included (0006). That's the one place `on_done`'s time is visible, because the access log and
  `Server-Timing` are written before it runs.
- **Not recorded:** WebSocket flows (long-lived by design, so they would dominate `total`) and relayed
  traffic (no stages).

### Buckets

These are fixed, with one set for network-scale times and a finer set for middleware:

```text
flow, connect, ttfb:  0.005 0.01 0.025 0.05 0.1 0.25 0.5 1 2.5 5 10 30 60 +Inf
middleware:           0.0001 0.00025 0.0005 0.001 0.0025 0.005 0.01 0.025 0.05 0.1 0.25 0.5 1 2.5 5 +Inf
```

- The network set is Prometheus's default set (0.005 to 10), plus 30 s and 60 s, because downloads and
  slow origins happen in a lab.
- The middleware set starts at 100 µs. Live middleware here costs 0.1–0.4 ms (0004, 0005), which would
  all land in the first bucket of the network set. It ends at 5 s, the default
  `middleware.hook_timeout_ms`.

### Series count

This is fixed by configuration, not by traffic:

| Metric | Series |
|---|---|
| flow duration | 5 outcomes × (14 buckets + sum + count) = 80 |
| connect | 2 × 16 = 32 |
| ttfb | 16 |
| middleware | 18 per middleware in the chain |

That's 128 series, plus 18 per middleware.

### Implementation

- A `Histogram` class next to `Counter` and `Gauge` in `metrics.py`. It keeps a list of counts per label
  set, plus the sum and the count, and renders the cumulative text format. It's about 40 lines and
  needs no dependency.
- `exchange.handle` observes the four values from `flow.timings` after `on_done`, so the middleware
  histogram includes it.
- `ExchangeContext` already carries `metrics`.

## Alternatives considered

- **One histogram with a `stage` label** (`stage="total|connect|ttfb|middleware"`). Rejected: the stages
  need different buckets (100 µs middleware next to 60 s downloads), connect needs `reused`, and
  middleware needs its name. A shared label set would be mostly empty combinations.
- **Summaries (client-side quantiles).** Rejected: quantiles can't be added up across instances, which
  the fleet view (0002) needs, and they need sliding windows in the proxy.
- **Native (sparse) histograms.** Rejected for now: they need the protobuf exposition format and a
  dependency. Classic histograms can be scraped by everything.
- **`prometheus_client`.** Rejected: a dependency for about 40 lines, and its process collectors add
  series nobody asked for. 0001 and 0002 chose hand-written exposition.
- **Configurable buckets.** Not now: changing buckets breaks existing dashboards and recording rules.
  A setting can be added later if the fixed ones prove wrong.
- **A host label.** Rejected: unbounded series (`metrics.py`). Per-host detail is what the access log is
  for.
- **Body size histograms.** Not in this record. They're cheap to add the same way if someone needs
  them.

## Consequences

- **What it answers:** p50, p95 and p99 for flows, origins, new connections and each middleware, over
  any time window, and aggregated across a fleet. For example, the origin's p99:
  `histogram_quantile(0.99, sum by (le) (rate(mitm_proxy_origin_ttfb_seconds_bucket[5m])))`.
- **Scrape size:** `/metrics` grows by about 128 series plus 18 per middleware. That's small for
  Prometheus.
- **Cost:** about four bucket lookups per flow (a short loop over at most 16 boundaries), plus one per
  middleware.
- **Wiki:**
  - HTTP-API-Reference (the metrics table, with example queries);
  - Troubleshooting (the "Requests are slow" section: from one flow to the trend);
  - Architecture-Internals;
  - Deployment, if it shows a scrape config.

## Open questions

Resolved at approval (2026-10-28), as recommended:

1. **Four histograms.**
2. **`outcome`** labels the flow duration.
3. **Buckets** as proposed, up to 60 s.
