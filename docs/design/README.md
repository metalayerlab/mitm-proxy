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
| [0007](0007-flow-duration-histograms.md) | Flow duration histograms | Implemented | Four classic Prometheus histograms, hand-written: flow duration by `outcome`, upstream connect by `reused`, origin ttfb, and per-middleware time; fixed buckets; intercepted HTTP flows only |
| [0008](0008-adopt-interpose.md) | Adopt interpose for settings and secrets | Implemented | The settings engine and `Secret` come from interpose v0.1.0 (a pinned git tag); `config.py` keeps the keys, variables and project rules; `proxy.auth` and `ca.key` become `Secret`; coercion becomes strict |
| [0009](0009-adopt-interpose-metrics.md) | Adopt interpose metrics | Implemented | Metric types and the text format come from interpose (v0.1.2); every label's values are declared up front, the middleware label from `middleware.available` at build and reload; `/metrics` output unchanged byte for byte |
| [0010](0010-adopt-interpose-pipeline-core.md) | Adopt interpose pipeline core | Implemented | `FlowPipeline` wraps interpose's `Run` with mitm-proxy's policy (fail open, chain order, no response hooks after local answers, 5 s default timeout); middleware may set `on_error = "fail"` or `timeout`; a fail-closed failure is the phase's deny (403/502) |
| [0011](0011-fail-closed-502-and-async-hooks.md) | A 502 for a fail-closed failure, and async hooks checked at startup | Implemented | Supersedes 0010's fail-closed statuses: a `HookFailed` is `502 middleware <name> failed` in both phases (a deliberate deny unchanged); a sync hook is a startup or reload error via interpose's `require_async`; hook timeouts are per call and cancel the hook; with interpose v0.1.4 a late result (overran, swallowed) is a timeout failure |
| [0012](0012-capture-and-reverse-proxy.md) | Capture middleware and reverse-proxy mode | Proposed | A plain-HTTP `reverse` listener to one fixed upstream on the same exchange path (`flow.direction` inbound/egress); `middleware.config.<name>` passed to the constructor (interpose section settings, `{secret_file}` leaves); a built-in opt-in `capture` middleware writing redacted JSONL per rule per UTC day from `on_done`; `observe_only` middleware stream unbuffered; `upstream.read_timeout_ms` (default 120000) |
