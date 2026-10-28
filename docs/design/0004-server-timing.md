# 0004: Server-Timing on intercepted responses

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Proposed
Date: 2026-10-28
Authors: Stephen Cox; drafted by Claude (Claude Code)

## Context

Design 0003 put stage timings on every flow (`flow.timings`) and in the flow log. The in-depth examples
then had a lab middleware copy them into a `Server-Timing` response header (W3C Server Timing). Browser
devtools show that header in each request's Timing tab, so the person at the browser can see, per
request, how long the proxy waited for an upstream connection, how long the origin took, and how long
middleware took, without reading the proxy's logs. This record makes that a built-in feature.

The lab version showed two limits of doing it as middleware:

- To add a header to every response, the middleware must apply to every flow. Every flow it applies to
  has its bodies buffered for middleware (up to `body.max_buffer_bytes`, about twice that in memory for
  a moment; 0001). A timing header would cost a full buffer per flow.
- A response that middleware answers itself (`STOP` with `flow.response`, or `DENY`) skips every
  `on_response` hook (0001). Cache hits, local answers and blocks, which are the responses where the
  timing is most interesting, got no header.

Constraints: off by default (by default the proxy changes nothing that 0001 doesn't require); no body
buffering; no new dependency; the header must not break an origin's own `Server-Timing`.

## Decision

### The setting

| Setting | Type | Default | Secret |
|---|---|---|---|
| `proxy.server_timing` | bool | `false` | no |

`MITM_PROXY_PROXY_SERVER_TIMING`, like every setting (0001). When it's on, the proxy adds a
`Server-Timing` field to the response head of every **intercepted** HTTP flow:

- TLS-intercepted flows, and plain-HTTP flows to a listed host;
- whether or not any middleware applies (no buffering is added: the header is written by the exchange
  when the response head goes out, not by a middleware);
- responses from the origin, local answers (`STOP` with a response), `DENY` responses, and the proxy's
  own `502`/`504` errors.

Never on relayed tunnels (they aren't decrypted), and never on WebSocket upgrades.

### The header

One extra `Server-Timing` field line, added after the origin's own, so an origin's metrics are
untouched (the header is a list; field lines combine):

```text
server-timing: mitm-connect;dur=0.0;desc="reused", mitm-origin;dur=79.3, mitm-mw;dur=0.4, mitm-proxy;dur=80.1
```

| Metric | From `flow.timings` | When present |
|---|---|---|
| `mitm-connect` | `connect`, `desc="reused"` or `"new"` | an upstream connection was leased |
| `mitm-origin` | `ttfb` | the origin answered |
| `mitm-mw` | `middleware_total` up to the response head | middleware applied |
| `mitm-local` | `desc="<middleware>"` (no duration) | middleware answered or denied the request |
| `mitm-proxy` | `started` to the response head going out | always |

- **Prefix:** `mitm-`, so the names can't collide with an origin's metrics (`db`, `cache`, `app` are
  common).
- **Durations** are in milliseconds with one decimal, as the spec expects.
- **`mitm-proxy`, not "total":** a response header is sent before the body, so the header can only cover
  the time to the response head. Body transfer time isn't in it; the browser measures that itself
  (Content Download).
- **Not `Timing-Allow-Origin`:** the proxy never adds it, so cross-origin pages' scripts can't read these
  values. Same-origin scripts can (`PerformanceResourceTiming.serverTiming`); see Consequences.
- **Middleware can't see or remove the field:** it's added after the response hooks, at the same point
  as the framing headers.

### Where

`exchange.py` builds the field from `flow.timings` just before `responder.start(...)`, in `_respond`
and in `send_simple` (local errors and denies), when the setting is on and the flow is intercepted.
`ExchangeContext` gets a `server_timing: bool` taken from the configuration snapshot, so a future
managed-config change (0002) applies to new connections like every other setting.

## Alternatives considered

- **A built-in `server_timing` middleware** in the registry (the lab version). Rejected: it buffers every
  flow it applies to, and it can't touch local answers or denies.
- **Always on.** Rejected: it tells every page it was intercepted, and changes every response by default.
- **The full total as a trailer** (`Server-Timing` is allowed in trailers). Rejected: HTTP/1.1 clients
  only get trailers if they asked (`TE: trailers`), and browsers don't reliably show trailer metrics.
- **Per-middleware metrics** (`mitm-mw-cache;dur=…`). Rejected for now: it puts every middleware name in
  every response. The total plus `mitm-local` for the one that answered is enough; the breakdown is in
  `flow.timings.middleware` and could be added later behind its own option.
- **Unprefixed names** (`connect`, `origin`). Rejected: they collide with origins' own metrics.

## Consequences

- With the setting on, any browser's devtools shows where each request's time went, including cache hits
  and blocks answered by middleware, with no buffering cost.
- **Exposure:** with the setting on, every intercepted response announces the proxy, and a page's own
  scripts can read the timings and report them to the site (for example, through analytics). That's
  acceptable for a lab tool turned on deliberately, and is why it's off by default. Security says so.
- Responses get one header of about 120 bytes.
- Rollback: turn the setting off. Nothing is persisted.
- **Wiki:** In-Depth-Configuration (prose for the setting; the table is generated), Security (exposure),
  Troubleshooting (reading the breakdown in devtools), Writing-Middleware (the field is added after
  the hooks), Architecture-Internals (data flow).

## Open questions

1. **Name and place.** `proxy.server_timing` (the proxy's behaviour towards clients), or a new
   `response.` section in case more response-side options follow?
2. **`mitm-local` names the middleware** (`desc="cache"`). Useful in devtools, but it shows middleware
   names to clients. Keep it, or say only `desc="middleware"`?
3. **`mitm-proxy`.** Keep it (the time to the response head, which is easy to misread as a total), or
   leave it out and rely on the browser's own timings?
