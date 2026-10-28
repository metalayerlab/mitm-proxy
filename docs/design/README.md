# Design records

Why the code is the way it is. Copy [0000-template.md](0000-template.md) to `NNNN-short-title.md` before a
new feature, a new dependency, or a public-interface change, and add a line below. Status moves Proposed →
Approved (by a human) → Implemented; supersede an implemented record with a new one, never rewrite it.

**Index:** read this first, then open only the records relevant to your task. CI checks that every record is listed.

| # | Title | Status | Decision in one line |
|---|---|---|---|
| [0001](0001-initial-design.md) | Initial design | Approved | Explicit CONNECT proxy behind an InterceptionSource seam; h1+h2 via h11/h2; allowlist-only interception; protocol-agnostic Flow with chunk-capable middleware hooks; pooled, always-verified upstream TLS |
| [0002](0002-control-plane-management.md) | Control-plane management | Proposed | Instances pull config from a central control plane over one long-polled HTTPS exchange (OpAMP concepts, JSON); bootstrap vs managed settings; mTLS enrollment, fleet intermediate CA; local API read-only: healthz, readyz, /v1/status, /metrics, /ca.* |
| [0003](0003-flow-timings.md) | Flow timings | Implemented | Read-only `flow.timings` (monotonic stage times, per-middleware time) filled by the exchange; FlowLogger appends total/connect/ttfb/mw |
| [0004](0004-server-timing.md) | Server-Timing on intercepted responses | Implemented | Opt-in `proxy.server_timing`: the exchange adds a `mitm-`-prefixed `Server-Timing` field (connect, origin, mw, local, proxy) to every intercepted response head, local answers and errors included, without buffering |
| [0005](0005-access-log.md) | A built-in access log | Implemented | `proxy.access_log` (intercepted flows, with counted sizes, true totals and an `outcome`) and `proxy.access_log_relayed` (unintercepted tunnels and plain HTTP), written by the proxy; the `log` middleware is removed |
| [0006](0006-on-done-hook.md) | The on_done middleware hook | Implemented | `on_done(flow)`: once per flow, after it has ended however it ended (local answers, denies, errors, aborts), observe-only, under the hook timeout; read-only `flow.outcome` |
| [0007](0007-flow-duration-histograms.md) | Flow duration histograms | Approved | Four classic Prometheus histograms, hand-written: flow duration by `outcome`, upstream connect by `reused`, origin ttfb, and per-middleware time; fixed buckets; intercepted HTTP flows only |
