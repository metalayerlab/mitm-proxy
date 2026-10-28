# 0005: A built-in access log

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Proposed
Date: 2026-10-28
Authors: Stephen Cox; drafted by Claude (Claude Code)

## Context

The flow log is a middleware (`log`, `FlowLogger`; 0001), and the in-depth examples showed that it can't
see everything:

1. **Local answers are missing.** A `STOP` with a response, or a `DENY`, skips every `on_response` hook
   (0001), so cache hits, local answers and blocks get no log line.
2. **Rewrites by chunk hooks are missing.** `on_response` runs before the chunk hooks, so the line shows
   the body's size before a chunk hook rewrote it, and leaves out that hook's time. Its `total` ends at
   `on_response`, not at the last byte (0003).
3. **It buffers everything.** `log` applies to every flow, and every flow a middleware applies to has its
   bodies buffered (0001). Turning logging on costs up to `body.max_buffer_bytes` per flow.
4. **Proxy errors and aborted flows are missing.** A `502`/`504` from the proxy, or a client that resets
   the stream, never reaches `on_response`.

0004 had the same problems with Server-Timing and fixed them by having the exchange write the header
itself. This record does the same for the log. There's no release yet, so `log` can be replaced rather
than deprecated.

Also, nothing records traffic the proxy relays without intercepting it. Some users want that (who used
the proxy, for what), others find it noise. It's the user's choice, so it's a separate setting.

## Decision

### Settings

| Setting | Type | Default | Secret |
|---|---|---|---|
| `proxy.access_log` | bool | `false` | no |
| `proxy.access_log_relayed` | bool | `false` | no |

The two are independent.

- `proxy.access_log`: one line per **intercepted** flow: HTTP flows, and WebSocket flows to listed hosts.
- `proxy.access_log_relayed`: one line per flow the proxy **relays without intercepting**: tunnels to
  unlisted hosts, non-TLS tunnels, tunnels whose SNI isn't listed, and plain-HTTP requests to unlisted
  hosts.

### Intercepted flows (logger `mitm_proxy.flows`, INFO)

Written by the exchange once the flow has ended, in the same `finally` that stamps
`flow.timings.complete`. The line is a synchronous `log` call, so it's also written when the flow's task
is cancelled (an HTTP/2 stream reset, or shutdown after the grace period).

```text
9eba5076… h2>h2 GET https://example.org/a.png 200 req=0 resp=21292 total=79.6ms connect=0.0ms(reused) ttfb=79.3ms mw=0.1ms outcome=origin
```

The fields are the same as today's `log` line, in the same order, with `outcome=` added at the end. Two
fields change meaning, and both become more accurate:

- **`req` and `resp` are counted bytes**: body bytes read from the client, and body bytes written to the
  client (after framing is removed). A body that was streamed and not buffered now gets a number
  instead of `-`, and a body that a chunk hook rewrote shows its rewritten size.
- **`total`** runs from `started` to `complete`, the last byte sent. `mw` includes every hook, chunk
  hooks too. `mw` doesn't include `on_done` (0006), which runs after the line is written, so the log
  never waits on middleware.

`outcome` says how the flow ended:

| `outcome` | Meaning |
|---|---|
| `origin` | The origin's response was sent |
| `local:<name>` | Middleware `<name>` answered (`STOP` with a response) |
| `denied:<name>` | Middleware `<name>` denied it (`403` on the request, `502` on the response) |
| `error:<status>` | The proxy answered with its own error: `502`, `504`, or `502` for a `STOP` without a response |
| `aborted` | The flow ended before the response was complete: client reset or disconnect, or an I/O error |
| `websocket` | A WebSocket flow, logged when the connection closes; `req`/`resp` count the bytes each way |

For `aborted`, the status is the one already sent, or `-`.

### Relayed flows (logger `mitm_proxy.relayed`, INFO)

```text
tunnel 4f0c… 192.0.2.10:51234 example.net:443 out_of_scope up=1830 down=48211 total=5012.3ms
http   7a1d… 192.0.2.10:51240 GET http://example.net/ 200 req=0 resp=1256 total=88.1ms
```

- **Tunnels**, one line when they close. The reason is the decision, with the same values as the
  `mitm_proxy_tunnels_total` metric's label: `out_of_scope`, `not_tls`, `sni_out_of_scope`. If the
  proxy couldn't connect, it's `error:<status>`. `up` and `down` are the bytes from the client and to
  the client, which `relay()` already counts. Hostnames come from the CONNECT target. Nothing inside an
  undecrypted tunnel is visible.
- **Plain-HTTP requests to unlisted hosts**: method, URL, status, body bytes and duration. This traffic
  is already readable in plain text, but no middleware runs on it, so there are no timings beyond
  `total`.

### The `log` middleware goes

`FlowLogger`, `mitm_proxy/middleware/log.py` and the built-in registry entry are removed, so
`middleware.available` starts empty. A chain that names `log` fails at startup like any unknown name.
The error message says to use `proxy.access_log` instead. Writing-Middleware keeps a logging example,
rewritten for `on_done` (0006).

### Where

- **The byte counts:**
  - a counter wraps the client's request body iterator;
  - another counts `responder.data` calls in `_respond`;
  - `send_simple` knows its own length.
- **The outcome:** the exchange records it wherever it already chooses the path (origin, STOP, DENY,
  upstream error), and the `finally` turns an unfinished one into `aborted`.
- **The tunnel line:** `ProxyServer._relay` writes it from `relay()`'s return value. A tunnel that fails
  to connect logs in the existing failure branch.

The cost is two integer additions per body chunk and one log call per flow. No flow is buffered for
logging.

## Alternatives considered

- **Run `on_response` for local answers too.** Rejected: it changes what `STOP` means, and middleware
  that assumes the origin answered would act on local answers. A cache would re-store its own hits; a
  rewrite would rewrite a local answer.
- **Keep `log` as middleware, moved to `on_done` (0006).** Rejected as the main log: it would still
  buffer every flow, and it would still miss relayed traffic. `on_done` stays for middleware that needs
  to act once a flow has finished.
- **One setting for both kinds of traffic.** Rejected: relayed traffic is the noisy part, and whether to
  record it is the user's choice.
- **Structured (JSON) lines.** Not now. The positional `key=value` line matches the existing log, and a
  JSON formatter can be added later as a log-format option for all logs, not just this one.

## Consequences

- **What's logged:** every intercepted flow, including local answers, denies, proxy errors and aborted
  ones, with accurate sizes and true totals. Relayed traffic can be logged too if the user chooses.
- **Memory:** logging no longer costs memory.
- **Config change:** `middleware.chain: [log]` stops working (no release has shipped it). Local configs
  move to `proxy.access_log: true`.
- **Personal data:** relayed logging puts every destination host and client address in the log. It's off
  by default; Security says so.
- **Wiki:**
  - In-Depth-Configuration (the two settings, the line formats, `outcome`);
  - Quick-Start and Home (the example line and how to turn it on);
  - Troubleshooting (reading `outcome`; the "Requests are slow" section);
  - Writing-Middleware (no built-in `log`);
  - Security (relayed logging);
  - Architecture-Internals.

## Open questions

1. **`req`/`resp` meaning.** Bytes read from the client and written to the client, as proposed. The
   alternative is the body sizes that went upstream and came back from the origin. Both are useful;
   the client's side matches what a user sees.
2. **Logger names.** `mitm_proxy.flows` (kept, since people already filter on it) and
   `mitm_proxy.relayed`?
3. **Remove `log` outright**, as proposed, or keep it for one release as an alias that turns on
   `proxy.access_log` with a deprecation warning?
