# 0003: Flow timings

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Proposed
Date: 2026-10-28
Authors: Stephen Cox; drafted by Claude (Claude Code)

## Context

The first live test (Emby and Sonarr through a local container) logged a single duration per flow: from
request headers to the end of the response. The 99th percentile was 746 ms, but nothing said where that
time went: waiting for an upstream connection, the origin thinking, middleware, or the proxy itself. Nobody
can tell whether the proxy is slow or the origin is.

Middleware authors want the same numbers (for example, flagging slow origins, or timing their own hooks).
So the timings have to be on the `Flow`. That is the middleware API, a public interface under 0001, and
this record exists for that reason.

Constraints: the cost must be negligible (a few monotonic clock reads per flow); `Flow` stays plain data
(0001); no new dependency.

## Decision

### `Flow.timings`

A new attribute, `flow.timings: Timings`, filled in by the exchange as the flow progresses. Middleware
reads it and does not write it.

```python
@dataclass
class Timings:
    started: float  # loop.time() when the request headers arrived
    request_complete: float | None  # request body read (buffered or passed on)
    upstream_acquired: float | None  # a pooled connection was leased (or opened)
    request_sent: float | None  # the request (head and body) went upstream
    response_headers: float | None  # the origin's response headers arrived
    complete: float | None  # the last byte went to the client
    connection_reused: bool = False
    middleware: dict[str, float]  # seconds spent in each middleware's hooks, by name

    # Derived, in seconds; None when a stage didn't happen (e.g. DENY, or a STOP with a response)
    @property
    def connect(self) -> float | None: ...  # request_complete -> upstream_acquired
    @property
    def ttfb(self) -> float | None: ...  # request_sent -> response_headers: the origin's share
    @property
    def middleware_total(self) -> float: ...
    @property
    def total(self) -> float | None: ...  # started -> complete
```

- **Clock:** `loop.time()`, which is monotonic, so durations are right even if the wall clock changes.
  Values are plain floats, which keeps `Flow` serializable later.
- **Middleware time** is measured by the pipeline runner around each hook it awaits (all of a
  middleware's hooks for the flow, summed). A middleware that failed keeps the time it took up to the
  failure.
- **What "proxy overhead" means:** `total − connect − ttfb − middleware_total` is the remainder: the
  proxy's own work plus moving the bodies between client and origin. For a buffered flow that includes
  receiving the whole body from the origin. The log shows the parts and leaves the subtraction to the
  reader, so the record makes no claim that this remainder is pure overhead.

### The flow log line

`FlowLogger` appends the breakdown, in milliseconds, to its existing line:

```text
INFO mitm_proxy.flows: f601a181… h2>h2 GET https://example.org/ 200 req=0 resp=19 total=41.0ms connect=0.0ms(reused) ttfb=29.1ms mw=0.4ms
```

A stage that didn't happen prints `-`. The existing fields keep their order, so anything parsing the
current line by position still works. Only new fields are added, at the end.

### Not in this record

- **Duration metrics** (a Prometheus histogram such as `mitm_proxy_flow_duration_seconds` by stage).
  `metrics.py` writes only counters and gauges today, and a histogram needs bucket choices. See open
  question 1.
- **Per-middleware times in the log line:** the log shows the total only; the breakdown is on
  `flow.timings.middleware` for anyone who wants it.
- **Timings for tunnels and WebSocket flows:** they have no request/response stages.

## Alternatives considered

- **A DEBUG log line from the exchange, with no API change.** Rejected. It splits one flow across two log
  lines, and middleware can't use the numbers.
- **Timestamps in `flow.metadata`.** Rejected. `metadata` belongs to middleware, and keys like
  `"_ts_ttfb"` are an undocumented API that's easy to clobber.
- **Wall-clock timestamps (`datetime`).** Rejected: not monotonic. One wall-clock `started_at` could be
  added later if flows are ever stored.
- **Timing every hook separately (`on_request`, `on_response_chunk`, …).** Rejected for now. The total
  per middleware answers "which middleware is slow"; per-hook detail can be added later without breaking
  anything.

## Consequences

- Middleware and logs can tell a slow origin from a slow proxy or a slow middleware.
- The middleware API grows by one read-only attribute and one class (`mitm_proxy.http.flow.Timings`). The
  change is additive: existing middleware is unaffected.
- The flow log line gets longer. Anyone parsing it by position is unaffected; anyone matching the whole
  line exactly will need to update.
- Cost: about six clock reads and two dictionary updates per flow. That's negligible next to TLS and
  HTTP parsing.
- **Wiki:** Writing-Middleware (the `Flow` table and a timing example), Architecture-Internals (data
  flow), Quick-Start and Home (the example log lines), Troubleshooting (reading the breakdown).

## Open questions

1. **Histogram metric.** Add `mitm_proxy_flow_duration_seconds{stage="total|connect|ttfb|middleware"}`
   now, with fixed buckets (5 ms … 10 s), or leave metrics for a later record?
2. **Field names.** `ttfb` is jargon, though widely known. Is it acceptable, or would you prefer
   `origin` (meaning "the origin's share")?
3. **The log line.** Is appending `total=… connect=… ttfb=… mw=…` acceptable, or would you rather it
   switch to `key=value` throughout (a breaking change for anyone parsing it by position)?
