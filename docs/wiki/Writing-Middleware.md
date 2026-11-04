# Writing Middleware

Middleware is how you inspect and change traffic. It sees one `Flow` per request (one HTTP/1.1 request, or one HTTP/2 stream), the same whichever protocol each side speaks.

## A first middleware

```python
# labtools/middleware.py
from mitm_proxy.http.flow import Flow, Response
from mitm_proxy.pipeline import Action, Middleware


class BlockAds(Middleware):
    """Refuse requests to /ads/, and tag every response."""

    def applies_to(self, flow: Flow) -> bool:
        return flow.host.endswith("example.org")

    async def on_request(self, flow: Flow) -> Action:
        if flow.request.path.startswith("/ads/"):
            return Action.DENY
        return Action.CONTINUE

    async def on_response(self, flow: Flow) -> Action:
        if flow.response is not None:
            flow.response.headers.set("x-seen-by", "mitm-proxy")
        return Action.CONTINUE
```

Install the package where mitm-proxy runs, then register and enable it:

```yaml
middleware:
  available: [block-ads=labtools.middleware:BlockAds]
  chain: [block-ads]
```

The name (`block-ads`) is what appears in logs and metrics. The order of `chain` is the order of execution: there are no priorities.

## Options

A middleware takes options from `middleware.config.<name>` ([In-Depth Configuration](In-Depth-Configuration#options-for-a-middleware)). With an entry, mitm-proxy constructs your class with it as **one positional argument**: a read-only `Mapping` (nested mappings are read-only mappings, lists are tuples). With no entry, it's constructed with no arguments, so give the parameter a default if the options are optional:

```python
from collections.abc import Mapping
from typing import Any

from mitm_proxy.config import Secret


class StripTracking(Middleware):
    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        options = options or {}
        unknown = set(options) - {"params", "token"}
        if unknown:
            raise ValueError(f"unknown options: {', '.join(sorted(unknown))}")
        self.params = frozenset(options.get("params", ("utm_source",)))
        token = options.get("token")
        if token is not None and not isinstance(token, Secret):
            raise ValueError("token: write it as {secret_file: <path>}")
        self.token = token
```

```yaml
middleware:
  available: [strip=labtools.middleware:StripTracking]
  chain: [strip]
  config:
    strip: { params: [utm_source, gclid], token: { secret_file: /run/secrets/strip-token } }
```

- **Validate in the constructor.** Raise with a reason: startup fails with `middleware 'strip': … failed: <your reason>`, and a new configuration with bad options is refused, keeping the running one. Never put an option's value in the reason if it could be secret.
- **Secrets** arrive as `Secret` values (from `{secret_file: …}` leaves). Call `reveal()` only where you use the value; `str()` gives `********`.
- **Construction happens again on every applied configuration**, so don't keep state that must outlive one: a changed mapping applies to flows that start afterwards.
- A class that takes no argument but is given options fails startup, as does an entry for a name registered as an instance (`module:instance`): there's nothing to construct.

## The hooks

| Hook | Called | Returns |
|---|---|---|
| `applies_to(flow)` | Once, when the request headers arrive | `False`: no hooks of this middleware run for the flow, and its bodies aren't buffered for it |
| `on_request(flow)` | After the request body is read | an `Action` |
| `on_request_chunk(flow, chunk, is_last)` | For each chunk of a request that has a body | `None` (unchanged) or replacement `bytes` |
| `on_response(flow)` | After the response body is read | an `Action` |
| `on_response_chunk(flow, chunk, is_last)` | For each chunk of a response that may have a body | `None` or replacement `bytes` |
| `on_done(flow)` | Once, after the flow has ended, however it ended | nothing (observe only) |

Every hook except `applies_to` must be an `async def`: a plain `def` hook stops startup (or refuses a new configuration) with `middleware '<name>': <hook> must be an async function` (design 0011). In this version bodies are buffered, so a chunk hook is called once per body, with the whole body and `is_last=True`. Write chunk hooks as if chunks could be partial: a streaming release will call them per chunk without changing the signature.

## Actions

- `CONTINUE`: on to the next middleware.
- `STOP`: end this phase; the flow stands as it is. In `on_request`, set `flow.response` first (e.g. `Response.make(200, b"cached")`) to answer without contacting the origin; a STOP without a response is answered with 502.
- `DENY`: refuse. On the request, the client gets `403 denied by middleware <name>`; on the response, `502` with the same message.

## The Flow

| Field | |
|---|---|
| `flow.id` | Unique per request |
| `flow.type` | `FlowType.HTTP` (WebSocket flows never reach middleware) |
| `flow.protocol` | The client's protocol, `"http/1.1"` or `"h2"` (read-only) |
| `flow.direction` | `"inbound"` on a reverse listener, `"egress"` on the forward proxy (read-only) |
| `flow.host`, `flow.port`, `flow.method` | Shortcuts into the request |
| `flow.request` | `method`, `scheme`, `authority`, `path`, `headers`, `body`, `trailers` |
| `flow.response` | `status`, `reason`, `headers`, `body`, `trailers` (`None` before the origin answers) |
| `flow.client_conn` | Client address, SNI, offered ALPN, TLS version and cipher |
| `flow.server_conn` | Origin host, port, address, and the protocol on that side |
| `flow.metadata` | A dict for your own state between hooks |
| `flow.timings` | When each stage happened (read-only; see [Timings](#timings)) |
| `flow.outcome` | How the flow ended (read-only; `None` until it has; see [When a flow ends](#when-a-flow-ends)) |

`flow.direction` tells inbound traffic (a reverse listener, in front of a service) from egress (the forward proxy, a client reaching out); `applies_to` can select on it. Don't confuse it with the `direction` label on the `mitm_proxy_oversize_passthrough_total` metric, which is `request` or `response` — a different thing.

Headers are a case-insensitive multimap with lower-case names (`get`, `get_all`, `set`, `add`, `remove`). Connection-level headers (`Connection`, `Transfer-Encoding`, `Keep-Alive`, `Proxy-*`…) are already removed, and `Host` / `:authority` is `request.authority`, so you never deal with protocol differences. Bodies are de-framed but not decompressed: check `content-encoding` before reading text. Read a whole body with `await flow.request.body.read_all()`, or replace it with `flow.response.body = BufferedBodyStream(b"…")`. You can also change `flow.response.status`, or replace `flow.response` entirely. The framing follows the response you leave: a status that can't have a body (`1xx`, `204`, `304`, or any response to `HEAD`) is sent without one, even if the origin's had one.

## Timings

`flow.timings` is filled in by the proxy as the flow goes. The stamps are seconds on the `time.monotonic()` clock (compare them with each other or with `time.monotonic()`, never with wall-clock time). A stage that hasn't happened yet, or never will (a `DENY`, a `STOP` with your own response, an upstream error), is `None`.

| Stamp | When |
|---|---|
| `started` | The request headers arrived |
| `request_complete` | The request body was read |
| `upstream_requested`, `upstream_acquired` | A connection was asked of the pool, and leased or opened (`connection_reused` says which) |
| `request_sent` | The whole request went to the origin |
| `response_headers` | The origin's response headers arrived |
| `complete` | The last byte went to the client (after every hook has run, so hooks never see it set) |

With `proxy.server_timing` on, the proxy adds its own `Server-Timing` field to the response head after your response hooks have run, so you never see it in `flow.response.headers` and can't remove it. An origin's own `Server-Timing` is an ordinary header you can read or change.

The derived durations, in seconds, are `connect` (waiting for an upstream connection), `ttfb` (the origin's share: request sent to response headers), `middleware_total` and `total`. `timings.middleware` holds the time each middleware has spent in its hooks so far, by name.

```python
import logging
import time


class SlowOrigins(Middleware):
    async def on_response(self, flow: Flow) -> Action:
        ttfb = flow.timings.ttfb
        if ttfb is not None and ttfb > 1.0:
            so_far = time.monotonic() - flow.timings.started
            logging.getLogger(__name__).warning(
                "%s took %.1fs to answer (%.1fs so far)", flow.host, ttfb, so_far
            )
        return Action.CONTINUE
```

## When a flow ends

`on_done(flow)` is called once for every flow your middleware applied to, after the flow has ended: the last response byte has gone to the client, or a local answer, deny or proxy error has been sent, or the flow was aborted. It runs even for flows other hooks never saw: a response your middleware (or another one) answered with `STOP`, a `DENY`, and bodies too large to buffer. It's the place to record or export flows.

`flow.outcome` says how the flow ended, and `flow.timings.complete` is set:

| `outcome.kind` | `outcome.by` | `outcome.status` |
|---|---|---|
| `"origin"` | `None` | The origin's status |
| `"local"` | The middleware that answered | Its status |
| `"denied"` | The middleware that denied, or the fail-closed middleware that failed | `403` (request deny), `502` (response deny, or a fail-closed failure in either phase) |
| `"error"` | `None`, or the middleware that stopped without a response | `502` or `504` |
| `"aborted"` | `None` | The status already sent, or `None` |

`str(flow.outcome)` is the access log's form: `origin`, `local:cache`, `error:502`.

```python
import json
import logging

from mitm_proxy.http.flow import Flow
from mitm_proxy.pipeline import Middleware

audit = logging.getLogger("labtools.audit")


class Audit(Middleware):
    """One JSON record per flow to the example.org API, however it ended."""

    def applies_to(self, flow: Flow) -> bool:
        return flow.host == "api.example.org"

    async def on_done(self, flow: Flow) -> None:
        t = flow.timings
        record = {
            "flow": flow.id,
            "method": flow.method,
            "path": flow.request.path,
            "outcome": str(flow.outcome),
            "status": flow.outcome.status if flow.outcome else None,
            "total_ms": round((t.total or 0) * 1000, 1),
            "middleware_ms": {k: round(v * 1000, 1) for k, v in t.middleware.items()},
        }
        audit.info(json.dumps(record))
```

- **Observe only.** The response has gone, so changing `flow` has no effect.
- **Keep it fast.** On HTTP/1.1, the client's next request on the same connection waits until `on_done` returns. Hand slow work (files, network) to a queue that your middleware drains itself.
- **It always runs, once.** That includes flows the client cancelled (common in browsers) and flows still in flight when the proxy shuts down, bounded by `middleware.hook_timeout_ms`. It's skipped only if your middleware already failed earlier in the same flow.
- Its time is added to `flow.timings.middleware`, but the access log and `Server-Timing` are written before it runs, so they don't include it.

## Rules

- **Never block the event loop.** No `time.sleep`, blocking I/O or long CPU work in a hook: the whole proxy waits. A timeout can't interrupt synchronous code: a hook that blocks holds up the whole proxy until it returns. If it returns after its timeout, its result is thrown away and it counts as a timeout failure (logged `late: overran`), skipped unless it fails closed. Move blocking or CPU-bound work off the loop with `await asyncio.to_thread(...)`; the timeout then applies to the `await`, but the thread itself keeps running after a timeout until it finishes.
- **The timeout is per hook call.** `middleware.hook_timeout_ms` (5 s by default) or your `timeout` bounds each call: every `on_request`, every `on_response`, every chunk-hook call and `on_done`, each on its own. It isn't a budget for the whole flow.
- **Be cancel-safe.** A timeout cancels your hook at whatever `await` it's in. Release what you hold in `try`/`finally` or `async with`, and never swallow `asyncio.CancelledError` (no bare `except BaseException:` that carries on): re-raise it. A hook that catches the cancellation and returns anyway is a timeout failure (logged `late: swallowed`), and its result is thrown away.
- **Expect to fail safely.** A hook that raises, returns the wrong type, or runs longer than `middleware.hook_timeout_ms`, is logged with your middleware's name and the flow ID, and your middleware is skipped for the rest of that flow. The traffic carries on without it, unless your middleware fails closed (below).
- **`on_error` and `timeout` are reserved attributes.** The pipeline reads them from your middleware (design 0010); don't use the names for anything else, such as an HTTP client's timeout. The middleware's name is always its key in `middleware.available` (or its built-in name, `capture`), whatever a `name` attribute says.

## Failing closed, and your own timeout

By default a middleware fails open, as above. A gate, say a blocklist whose crash would let traffic through, can fail closed instead:

```python
from mitm_proxy.pipeline import Middleware


class Blocklist(Middleware):
    on_error = "fail"  # a gate that fails open is no gate
    timeout = 0.5  # seconds per hook call, instead of middleware.hook_timeout_ms
```

- **`on_error = "fail"`:** when one of its hooks raises, times out or returns the wrong type, the flow ends with `502 middleware <name> failed`, in either phase (`applies_to`, `on_request`, `on_request_chunk`, `on_response` or `on_response_chunk`; design 0011). The body names the middleware and nothing else: never the exception or a value. A deliberate `DENY` from it is unchanged (`403` on the request), so a `403` is always a decision and a `502 middleware <name> failed` is always a crash. The outcome is `denied:<name>`, status `502`. The failure is still logged and counted in `mitm_proxy_middleware_failures_total`. If `applies_to` is the hook that failed, no middleware has been selected yet, so none gets `on_done` for that flow. Bodies over `body.max_buffer_bytes` still skip the body hooks, but a failed `applies_to` denies them too.
- **`on_done` always fails open,** whatever `on_error` says: the flow has already ended.
- **`timeout`:** seconds (a number above 0) for each of its hook calls; `NO_LIMIT` for none. A wrong value for either attribute stops startup with a one-line error naming the middleware.
- **Narrow with `applies_to`.** Every flow a middleware applies to is buffered, and a middleware with a chunk hook makes its flows' bodies go out chunked. Some origins refuse chunked uploads (`411`).
- **Bodies over `body.max_buffer_bytes`** skip middleware entirely and stream through.
- **Don't start background tasks per chunk.** Await your work in the hook; the pipeline relies on it for backpressure.

## The built-in capture middleware

`capture` ships with mitm-proxy (`mitm_proxy.middleware.capture:Capture`) and needs no `middleware.available` entry: put `capture` in `middleware.chain` and give it options in `middleware.config.capture` ([its options](In-Depth-Configuration#capture)). It records each flow one of its rules matches as one JSON object per line, written from `on_done`, so local answers, denies, proxy errors and aborted flows are recorded too:

```json
{"rule": "inbound-api", "direction": "inbound", "flow": "9eba5076…", "outcome": "origin",
 "request": {"method": "POST", "url": "http://api.internal:8000/v1/answer?api_key=[REDACTED]",
             "headers": [["content-type", "application/json"], ["authorization", "[REDACTED]"]],
             "body": "{\"question\": \"…\"}"},
 "response": {"status": 200, "headers": [["content-type", "application/json"]], "body": "{…}"},
 "timings": {"start": "2026-11-03T10:15:02.123456Z", "ttfb_ms": 812.4,
             "end": "2026-11-03T10:15:09.551021Z"},
 "streamed": false}
```

(Shown wrapped; on disk it's one line.)

- **`flow`** is the flow ID, to join with the access log; **`outcome`** is written as the access log writes it (`origin`, `local:<name>`, `denied:<name>`, `error:<status>`, `aborted`). **`direction`** is the flow's: `inbound` or `egress`. It's a different thing from the `direction` label (`request`/`response`) of `mitm_proxy_oversize_passthrough_total`, which says which body was too big.
- **Headers** are `[name, value]` pairs in order (a header can repeat). Trailers, when a message has them, are under `trailers`, redacted the same way.
- **Bodies** are as sent, after every middleware's chunk hooks. A body that is UTF-8 text is a string; any other body is base64, with `"body_encoding": "base64"`. A `gzip` or `deflate` body is decoded first, with `"content_decoded": "gzip"`; any other encoding is stored as it came, in base64. A body longer than `max_body_bytes` is cut and gets `"truncated": <its length>`. A body that skipped the hooks because it was over `body.max_buffer_bytes` is `null`, with `"omitted": "oversize"` (so is a compressed body that would decode to more than 64 MiB).
- **`response`** is `null` when the flow ended without one from the origin or a middleware (a deny, a proxy error).
- **`timings`:** `start` and `end` are UTC wall-clock times, `ttfb_ms` the origin's time to first byte, as in the access log; `null` for a stage that didn't happen.
- **`streamed`** is always `false` in this version: the bodies are buffered. A later release streams them past capture instead.

**Test your redaction.** `json_paths` are skipped silently on any body that isn't declared JSON (`application/json` or `*+json`) or doesn't parse, and on server-sent events (`text/event-stream`) for now. A rule that matches such bodies stores them with those values intact, and nothing warns you. Before you rely on a configuration, send it a request that carries a fake credential in every place you redact, and read the file back.

Capture fails open: a record it can't write is dropped and counted (`mitm_proxy_capture_records_total{rule, result}`), never an error for the traffic.
