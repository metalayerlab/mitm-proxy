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

## The hooks

| Hook | Called | Returns |
|---|---|---|
| `applies_to(flow)` | Once, when the request headers arrive | `False`: no hooks of this middleware run for the flow, and its bodies aren't buffered for it |
| `on_request(flow)` | After the request body is read | an `Action` |
| `on_request_chunk(flow, chunk, is_last)` | For each chunk of a request that has a body | `None` (unchanged) or replacement `bytes` |
| `on_response(flow)` | After the response body is read | an `Action` |
| `on_response_chunk(flow, chunk, is_last)` | For each chunk of a response that may have a body | `None` or replacement `bytes` |
| `on_done(flow)` | Once, after the flow has ended, however it ended | nothing (observe only) |

Every hook except `applies_to` is `async`. In this version bodies are buffered, so a chunk hook is called once per body, with the whole body and `is_last=True`. Write chunk hooks as if chunks could be partial: a streaming release will call them per chunk without changing the signature.

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
| `flow.host`, `flow.port`, `flow.method` | Shortcuts into the request |
| `flow.request` | `method`, `scheme`, `authority`, `path`, `headers`, `body`, `trailers` |
| `flow.response` | `status`, `reason`, `headers`, `body`, `trailers` (`None` before the origin answers) |
| `flow.client_conn` | Client address, SNI, offered ALPN, TLS version and cipher |
| `flow.server_conn` | Origin host, port, address, and the protocol on that side |
| `flow.metadata` | A dict for your own state between hooks |
| `flow.timings` | When each stage happened (read-only; see [Timings](#timings)) |
| `flow.outcome` | How the flow ended (read-only; `None` until it has; see [When a flow ends](#when-a-flow-ends)) |

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
| `"denied"` | The middleware that denied | `403` (request) or `502` (response) |
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

- **Never block the event loop.** No `time.sleep`, blocking I/O or long CPU work in a hook: the whole proxy waits. A hook that blocks can't be timed out.
- **Expect to fail safely.** A hook that raises, or runs longer than `middleware.hook_timeout_ms`, is logged with your middleware's name and the flow ID, and your middleware is skipped for the rest of that flow. The traffic carries on without it: don't use middleware to enforce security.
- **Narrow with `applies_to`.** Every flow a middleware applies to is buffered, and a middleware with a chunk hook makes its flows' bodies go out chunked. Some origins refuse chunked uploads (`411`).
- **Bodies over `body.max_buffer_bytes`** skip middleware entirely and stream through.
- **Don't start background tasks per chunk.** Await your work in the hook; the pipeline relies on it for backpressure.
