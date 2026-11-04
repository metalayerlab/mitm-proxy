# Architecture Internals

How mitm-proxy works inside: for contributors, reviewers and AI assistants. User-facing behaviour belongs on the other pages.

## Overview

One asyncio process runs two listeners: the **proxy** (`proxy.port`) and the read-only **admin API** (`listen.port`, FastAPI under uvicorn). A client connection enters through an *interception source*, which yields the target the client wants. One decision function says whether that tunnel is intercepted or relayed raw. Intercepted traffic is decrypted with a leaf certificate from the CA, parsed by the HTTP/1.1 (h11) or HTTP/2 (h2) layer into protocol-neutral flows, run through the middleware pipeline, and sent on through a pool of verified upstream connections.

```mermaid
flowchart LR
    subgraph process
        L[proxy listener] --> SRC[interception/explicit.py]
        SRC -->|CONNECT| CL[classify.py]
        SRC -->|http:// request| EX
        CL -->|relay| RL[relay.py]
        CL -->|intercept| TLS[tls/terminate.py + tls/ca.py]
        TLS --> H[http/h1.py · http/h2.py]
        H --> EX[exchange.py]
        EX --> PL[pipeline.py]
        EX --> POOL[upstream/pool.py]
        API[api.py] -.reads.-> SRV[server.py]
    end
    POOL --> O[(origins)]
    RL --> O
```

## Components

| Module | Responsibility |
|---|---|
| `mitm_proxy/__init__.py` | Package version |
| `mitm_proxy/config.py` | The settings, their checks and the rules that span settings, on [interpose](https://github.com/metalayerlab/interpose)'s engine (`_FILE` > env > file > default, strict types, `Secret`; design 0008). `load_config` reads each value through interpose's typed accessors (`r.str`, `r.int`, `r.bool`, `r.list`, `r.secret`), so a secret setting can only be read as a `Secret`, and `one_of` gives `log_level` its declared upper-case spelling |
| `mitm_proxy/cli.py` | The `mitm-proxy` command (Typer) |
| `mitm_proxy/api.py` | The HTTP API (FastAPI): health, readiness, version |

| `mitm_proxy/service.py` | The process: proxy listener and admin API on one loop; ordered shutdown on SIGTERM |
| `mitm_proxy/server.py` | `ProxyServer`: the configuration snapshot, the listener, tunnel handling, status |
| `mitm_proxy/interception/base.py` | `InterceptedConnection` and the `InterceptionSource` seam (Phase 2 adds a transparent source) |
| `mitm_proxy/interception/explicit.py` | The explicit-proxy source: CONNECT and absolute-form requests, proxy authentication |
| `mitm_proxy/interception/reverse.py` | The reverse-proxy source: a plain HTTP/1.1 listener, every request sent to one fixed upstream (design 0012) |
| `mitm_proxy/classify.py` | The one intercept-or-relay decision, and peeking the ClientHello |
| `mitm_proxy/scope.py` | The allowlist: host normalization and `*.suffix` matching |
| `mitm_proxy/relay.py` | Raw bidirectional relay with half-close and an idle timeout |
| `mitm_proxy/streams.py` | `ByteStream`: the one stream shape every layer uses |
| `mitm_proxy/tls/clienthello.py` | Parses SNI and ALPN from a ClientHello (pure function) |
| `mitm_proxy/tls/ca.py` | Root CA load/generate; leaf minting with one shared key; the LRU of server contexts |
| `mitm_proxy/tls/terminate.py` | TLS over memory BIOs, so peeked bytes can be replayed into the handshake |
| `mitm_proxy/tls/upstream.py` | The always-verifying upstream SSL contexts |
| `mitm_proxy/http/flow.py` | `Flow`, `Request`, `Response`, `ClientConn`, `ServerConn`, `FlowType` |
| `mitm_proxy/http/headers.py` | `Headers`: ordered, case-insensitive multimap |
| `mitm_proxy/http/body.py` | `BodyStream`, `BufferedBodyStream`, `PassthroughBody` |
| `mitm_proxy/http/normalize.py` | Header normalization; h2 pseudo-headers ⇄ h1 request line and Host |
| `mitm_proxy/http/framing.py` | Content-Length vs chunked, and the HEAD/1xx/204/304 rule |
| `mitm_proxy/http/messages.py` | What protocol layers hand the exchange: `IncomingRequest`, `Responder`, `UpstreamResponse` |
| `mitm_proxy/http/h1.py` | HTTP/1.1 on h11: client-facing server (incl. proxy mode) and upstream client |
| `mitm_proxy/http/h2.py` | HTTP/2 on h2: client-facing server and upstream client, flow control |
| `mitm_proxy/exchange.py` | One request: flow, middleware, framing, upstream, response; WebSocket hand-off |
| `mitm_proxy/pipeline.py` | The middleware API (`Middleware`, `Action`), mitm-proxy's pipeline policy, and `FlowPipeline`: thin wrappers over [interpose](https://github.com/metalayerlab/interpose)'s pipeline core, which guards every hook (timing, timeout, failure; design 0010) |
| `mitm_proxy/middleware/` | The built-in middleware, by name (`BUILTIN`), which `load_middleware` resolves when `middleware.available` has no entry; `capture.py`: the `capture` middleware, which selects in `applies_to`, builds, redacts and truncates its record in `on_done`, and appends it in a worker thread (`asyncio.to_thread`) under a per-file lock (design 0012) |
| `mitm_proxy/upstream/pool.py` | The connection pool and the TCP/TLS connector |
| `mitm_proxy/auth.py` | Basic proxy authentication |
| `mitm_proxy/metrics.py` | Which metrics exist, their buckets and their labels' declared values, on [interpose](https://github.com/metalayerlab/interpose)'s metric types and text format (design 0009) |
| `mitm_proxy/errors.py` | Exception types |

## Data flow

Following `curl --proxy … https://example.org/` with `example.org` listed:

1. **Accept.** `service.py` accepted the TCP connection; `ProxyServer` hands it to `ExplicitProxySource`, which reads HTTP/1.1 in proxy mode and checks `Proxy-Authorization` if `proxy.auth` is set (407 otherwise).
2. **CONNECT.** The source builds an `InterceptedConnection` (stream, target, `accept`/`reject`) and calls `ProxyServer.handle_tunnel`. It reads the configuration *snapshot* current now and keeps it for the whole connection.
3. **Decide.** `classify.decide` checks scope (unlisted: connect to the origin, answer 200 or 502/504, relay raw). Listed: answer 200, peek the ClientHello (`proxy.peek_timeout_ms`), decide again on the bytes and the SNI.
4. **TLS.** `CertificateAuthority.server_context(host)` returns a cached context, or mints a leaf (one signature: the leaf key is shared). `TLSStream` replays the peeked bytes into an `SSLObject` and completes the handshake; ALPN picks `H2Server` or `H1Server`.
5. **Flow.** The protocol layer turns each request into an `IncomingRequest` (normalized headers, a body iterator) and a `Responder`. `exchange.handle` creates the `Flow`, starts the pipeline (`applies_to` decided once), buffers bodies only if middleware applies (up to `body.max_buffer_bytes`, else passthrough), runs `on_request` and the chunk hooks.
6. **Upstream.** The pool returns a connection to `(host, port, SNI)`: an HTTP/2 one is shared by concurrent flows, an HTTP/1.1 one is leased to one request at a time. A reused HTTP/1.1 connection that fails before any response byte is retried once on a new connection, for idempotent requests with a replayable body.
7. **Back.** The response head becomes `flow.response`; `on_response` and the response chunk hooks run; `framing.plan` picks the framing; the responder writes it.

**Reverse listeners** (`reverse`, design 0012) are the other arrival path. `ProxyServer` binds one listener per entry at startup (a bind failure is a `StartupError`, and `/readyz` waits for all of them), and hands each connection to a `ReverseProxySource`. The source runs an `H1Server` with no proxy mode (origin-form requests, no CONNECT, no proxy auth), rewrites each request to the fixed upstream — the authority becomes the upstream's, `X-Forwarded-Host` (if absent) and `X-Forwarded-For` are set — and calls the same `exchange.handle` with an `ExchangeContext` whose `target` is the upstream and whose `direction` is `"inbound"`. So middleware, the access log, Server-Timing, metrics and upstream verification are exactly as above. A change to `reverse` applies on restart; a reload keeps the running listeners.

**Timings:** `Flow.timings` is stamped along the way on the `time.monotonic()` clock: by `exchange.py` at each stage (the request body read, the pool lease, the request sent upstream through a wrapper on the body iterator, the response head, the end), and by interpose's guard around every hook `FlowPipeline` awaits (per middleware, on `time.perf_counter()`; only the differences are used). That's a few clock reads per flow.

**Access log:** `exchange.handle` wraps the client's request body in a byte counter and the client's `Responder` in a `_Recorder` (status sent, body bytes, finished or not). The exchange marks the path it took (origin, local, denied, error), and the `finally` that stamps `complete` writes the line: a synchronous log call, so it's also written when the task is cancelled. An unfinished response is `aborted`. Relayed tunnels are logged from `ProxyServer._relay`, using the counts `relay()` returns.

**End of a flow:** the same `finally` sets `flow.outcome`, writes the access-log line, then awaits `FlowPipeline.run_done()` (`on_done` for each middleware that applied). It runs when the task is cancelled too: an HTTP/2 stream reset, or shutdown after the grace period. That's why `H2Server` awaits its cancelled stream tasks before returning, so shutdown waits for their `on_done` (each bounded by the hook timeout). Last, `Metrics.observe_flow` records the flow in the duration histograms, after `on_done` so the middleware histogram includes it. The text format (cumulative buckets, `_sum`, `_count`, and the counters and gauges) is interpose's `Registry.render()`. Every label value is declared before use: the fixed sets in `metrics.py`, and the middleware names from `middleware.available` and the capture rule names, declared by `ProxyServer._build` before each snapshot's pipeline exists (`_build` also hands each `Capture` the records counter). An undeclared value is a `ValueError` at the call, so a hostname can't become a label. With `proxy.server_timing` on, `exchange.server_timing` turns them into a `Server-Timing` field, added after the response hooks and the framing headers, just before `responder.start` (and in `send_simple` for denies and proxy errors).

**State** is in memory only: the snapshot, the CA's LRU of server contexts, the pool. The root CA is the only thing on disk (`<state_dir>/ca/`); the shared leaf key sits encrypted in a private temporary directory while the process runs.

**Failure:** middleware errors are contained per flow (the middleware is skipped, logged and counted); upstream errors become 502/504 responses; a broken client connection ends only that connection. HTTP/2 stream errors reset one stream, not the connection.

**Backpressure:** every write awaits `drain()`; HTTP/2 DATA is acknowledged only as it's consumed; middleware hooks are awaited in line, never as background tasks.

## Design decisions

Why things are the way they are is recorded in [docs/design/](https://github.com/metalayerlab/mitm-proxy/tree/main/docs/design). Rejected ideas and the reasons are in [TODO.md](https://github.com/metalayerlab/mitm-proxy/blob/main/TODO.md) under "Decided against".

From [0001: Initial design](https://github.com/metalayerlab/mitm-proxy/blob/main/docs/design/0001-initial-design.md), unless noted:

- **Allowlist, fail closed:** only listed hosts are intercepted; scope is checked before peeking, and an unlisted SNI is relayed.
- **The `InterceptionSource` seam:** nothing after it knows how a connection arrived, so transparent interception is one new source, and so is the reverse-proxy source (design 0012): it reuses the whole exchange, only fixing the target and the flow direction.
- **`Flow` per request/stream, protocol-neutral:** both legs negotiate their own protocol; middleware never sees frames.
- **Chunk-capable hooks from the start, buffered implementation for now.**
- **Framing from registration, not from what a hook did;** HEAD/1xx/204/304 never framed.
- **Middleware failure fails open,** with the middleware's name in the log; a middleware may opt in to failing closed (`on_error = "fail"`), which ends the flow with `502 middleware <name> failed` in either phase (`Outcome.failed`; designs 0010, 0011). Hooks are checked for `async def` when a middleware is registered (interpose's `require_async`), so a sync hook is a startup or apply error, never a per-flow failure. The hook timeout bounds each hook call and cancels it at its current `await`; a result that comes back after the limit anyway (sync code that overran, or a swallowed cancellation) is a timeout failure (interpose v0.1.4, `Failure.late`).
- **Upstream TLS is always verified;** extra CAs are added, never substituted.
- **Middleware by name from a local registry, an immutable config snapshot, `/v1/status` and `/metrics`:** from [0002: Control-plane management](https://github.com/metalayerlab/mitm-proxy/blob/main/docs/design/0002-control-plane-management.md) (Proposed; only these parts are built).
