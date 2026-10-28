# 0006: The on_done middleware hook

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Approved
Date: 2026-10-28
Authors: Stephen Cox; drafted by Claude (Claude Code)

## Context

Middleware can act while a flow is in progress (0001), but it isn't told when the flow is finished, or
how it ended. The in-depth examples needed that:

- **Logging and auditing.** A cache hit, local answer or block skips `on_response` (0001), so a
  middleware that records flows misses exactly the ones it caused.
- **Final numbers.** Body sizes after chunk-hook rewrites, and `flow.timings.complete`/`total` (0003),
  only exist once the last byte has gone.
- **Exporters.** A HAR writer, a metrics exporter, or a "slow origin" alert wants each flow once,
  complete.

0005 moves the proxy's own access log out of middleware. This record gives middleware the same
end-of-flow view.

## Decision

### The hook

```python
class Middleware:
    async def on_done(self, flow: Flow) -> None:
        """Called once, after the flow has ended, however it ended. Observe only."""
```

- **When:** after the flow has ended and its access-log line (0005) has been written:
  - the last response byte has gone to the client;
  - or a local answer, deny or proxy error has been sent;
  - or the flow was aborted.
  `flow.timings.complete` is set.
- **Who:** every middleware that applied to the flow (`applies_to` returned True), in registration
  order. That includes flows whose bodies went over `body.max_buffer_bytes` and skipped the other hooks.
  A middleware that already failed for this flow is skipped, as with every other hook (0001).
- **Not for:** WebSocket flows (no middleware runs for them; 0001), and relayed traffic.
- **Observe only:** the response has been sent, so changes to `flow` have no effect, and the return value
  is ignored. `flow.metadata` is still the middleware's own.
- **Like every hook:** it's awaited inline under `middleware.hook_timeout_ms`. Failures and timeouts are
  logged and counted in `mitm_proxy_middleware_failures_total`. Its time is added to
  `flow.timings.middleware`, after the access log and Server-Timing have been written, so neither shows
  it.
- **Mutation:** overriding `on_done` doesn't make a middleware mutation-capable (only chunk hooks do).
- **Cancellation:** when the flow's task is cancelled (an HTTP/2 stream reset, or the shutdown grace
  period running out), `on_done` still runs, bounded by the hook timeout, and the cancellation is
  re-raised afterwards. So every applied middleware is told once, and shutdown can take up to the grace
  period plus one hook timeout.

### `flow.outcome`

A read-only attribute, set before `on_done` runs (and `None` until the flow has ended):

```python
@dataclass(frozen=True)
class Outcome:
    kind: str  # "origin" | "local" | "denied" | "error" | "aborted"
    by: str | None = None  # the middleware, for "local" and "denied"
    status: int | None = None  # the status sent to the client, if one was
```

It's the same information as the access log's `outcome=` field (0005), so the two can't disagree. The
access log writes it as `origin`, `local:cache`, `error:502`.

### Cost

On HTTP/1.1, the next request on the same client connection waits until `on_done` has returned, because
requests on one HTTP/1.1 connection are served in turn. On HTTP/2, only that stream's task waits.
Writing-Middleware says to keep `on_done` fast and to hand slow work (files, network) to a queue the
middleware drains itself.

## Alternatives considered

- **Run `on_response` for local answers too.** Rejected in 0005: it changes what `STOP` means.
- **Call `on_done` from a background task**, so it never delays the connection. Rejected: 0001 forbids
  `create_task` for hooks (backpressure, ordering, and an unbounded number of tasks under load).
  A middleware that needs that can keep its own bounded queue.
- **An event bus or subscriber API** (flows published to listeners outside the pipeline). Rejected for
  now: a second extension mechanism beside middleware. `on_done` covers the known uses.
- **Call `on_done` for middleware that failed earlier in the flow.** Rejected: "skipped for the rest of
  the flow" stays simple and predictable.

## Consequences

- Middleware can log, audit or export every flow it applied to, including the ones it answered, with
  final sizes and timings.
- The middleware API grows by one optional hook and one read-only attribute (`flow.outcome`,
  `mitm_proxy.http.flow.Outcome`). Existing middleware is unaffected.
- A slow `on_done` slows its HTTP/1.1 connection. The hook timeout and the docs set the limit.
- **Wiki:**
  - Writing-Middleware: the hooks table, `flow.outcome`, a logging example to replace the removed
    `log` middleware, and the "keep it fast" rule;
  - Architecture-Internals: data flow and cancellation.

## Open questions

Resolved at approval (2026-10-28):

1. **`Outcome`** is a small frozen dataclass (`kind`, `by`, `status`).
2. **On cancellation**, `on_done` is bounded by the full hook timeout, like every hook.
3. **Order:** registration order.
