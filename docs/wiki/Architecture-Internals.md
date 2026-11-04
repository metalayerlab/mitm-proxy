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
    POOL --> O[(origins, or exits)]
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
| `mitm_proxy/http/body.py` | `BodyStream`, `BufferedBodyStream`, `PassthroughBody`, `StreamingBody` |
| `mitm_proxy/http/normalize.py` | Header normalization; h2 pseudo-headers ⇄ h1 request line and Host |
| `mitm_proxy/http/framing.py` | Content-Length vs chunked, and the HEAD/1xx/204/304 rule |
| `mitm_proxy/http/messages.py` | What protocol layers hand the exchange: `IncomingRequest`, `Responder`, `UpstreamResponse` |
| `mitm_proxy/http/h1.py` | HTTP/1.1 on h11: client-facing server (incl. proxy mode) and upstream client |
| `mitm_proxy/http/h2.py` | HTTP/2 on h2: client-facing server and upstream client, flow control |
| `mitm_proxy/exchange.py` | One request: flow, middleware, framing, upstream, response; WebSocket hand-off |
| `mitm_proxy/pipeline.py` | The middleware API (`Middleware`, `Action`), mitm-proxy's pipeline policy, and `FlowPipeline`: thin wrappers over [interpose](https://github.com/metalayerlab/interpose)'s pipeline core, which guards every hook (timing, timeout, failure; design 0010) |
| `mitm_proxy/middleware/` | The built-in middleware, by name (`BUILTIN`), which `load_middleware` resolves when `middleware.available` has no entry; `capture.py`: the `capture` middleware, which selects in `applies_to`, builds, redacts and truncates its record in `on_done`, and appends it in a worker thread (`asyncio.to_thread`) under a per-file lock (design 0012) |
| `mitm_proxy/upstream/pool.py` | The connection pool, keyed by `Target` (host, port, TLS, SNI and `via`, the exit), and the direct TCP/TLS connector |
| `mitm_proxy/upstream/chain.py` | Chaining through exits (design 0013): `HTTPConnectConnector`, `Socks5Connector`, and `RoutingConnector`, the proxy's connector, which picks one by the target's exit, or connects directly when it has none |
| `mitm_proxy/routing/__init__.py` | `Exit`, `Route` and `Router`: which exit each site leaves by, or why it is refused (design 0013, step 1: the first exit of `routing.default_via` for every site) |
| `mitm_proxy/auth.py` | Basic proxy authentication |
| `mitm_proxy/metrics.py` | Which metrics exist, their buckets and their labels' declared values, on [interpose](https://github.com/metalayerlab/interpose)'s metric types and text format (design 0009) |
| `mitm_proxy/errors.py` | Exception types |

## Data flow

Following `curl --proxy … https://example.org/` with `example.org` listed:

1. **Accept.** `service.py` accepted the TCP connection; `ProxyServer` hands it to `ExplicitProxySource`, which reads HTTP/1.1 in proxy mode and checks `Proxy-Authorization` if `proxy.auth` is set (407 otherwise).
2. **CONNECT.** The source builds an `InterceptedConnection` (stream, target, `accept`/`reject`) and calls `ProxyServer.handle_tunnel`. It reads the configuration *snapshot* current now and keeps it for the whole connection.
3. **Decide.** `classify.decide` checks scope (unlisted: connect to the origin, answer 200 or 502/504, relay raw). Listed: answer 200, peek the ClientHello (`proxy.peek_timeout_ms`), decide again on the bytes and the SNI.
4. **TLS.** `CertificateAuthority.server_context(host)` returns a cached context, or mints a leaf (one signature: the leaf key is shared). `TLSStream` replays the peeked bytes into an `SSLObject` and completes the handshake; ALPN picks `H2Server` or `H1Server`.
5. **Flow.** The protocol layer turns each request into an `IncomingRequest` (normalized headers, a body iterator) and a `Responder`. `exchange.handle` creates the `Flow`, starts the pipeline (`applies_to` decided once), buffers bodies only if middleware applies (up to `body.max_buffer_bytes`, else passthrough) and not all of it is observe-only (then the bodies stream; below), runs `on_request` and the chunk hooks.
6. **Upstream.** The pool returns a connection to `(host, port, SNI)`: an HTTP/2 one is shared by concurrent flows, an HTTP/1.1 one is leased to one request at a time. A reused HTTP/1.1 connection that fails before any response byte is retried once on a new connection, for idempotent requests with a replayable body.
7. **Back.** The response head becomes `flow.response`; `on_response` and the response chunk hooks run; `framing.plan` picks the framing; the responder writes it.

**The streaming path** (design 0012). `FlowPipeline.streams` is true when every middleware that applies to the flow is observe-only (`Middleware.observe_only`; `pipeline.Entry` keeps the flag, and `is_mutation_capable` is false for them, so `framing.plan` keeps the declared length). Then `exchange._exchange` neither buffers the request body nor calls the chunk hooks itself: `request.body` becomes a `StreamingBody` over `_observed(incoming.body, FlowPipeline.observe_request_chunk)`, and `on_request` runs on the head. `_observed` is an async generator: it yields each chunk to whoever writes it onward (the upstream layer's send loop), and only when that loop asks for the next chunk, so after this one was written, awaits the observers' chunk hook; when the source ends it makes one last call, `(b"", True)`. The hooks are awaited in the consumer's own loop, never a task, so a slow hook stops the reading: TCP's window and HTTP/2's flow control (received DATA is acknowledged as it is consumed) carry the backpressure to the peer. `_respond` does the same for the response: `on_response` runs on the head (a `DENY` is still a `502`: nothing has been sent), then the head goes out and the body is written through `_observed(…, observe_response_chunk)`. A `StreamingBody` refuses `read_all()`. The request's trailers and the response's are put on the flow once their body has ended, for `on_done`. If an `on_response` hook replaces the body by assignment, that body is sent as on a buffered flow and the observers get it as one chunk. On a buffered (mixed) flow, `FlowPipeline._chunk` folds the body through the middleware that aren't observe-only, then shows the result to the observe-only ones (`_observe`), so they see the body as sent. An observe-only hook that returns bytes fails with `_NotObserving` (an `InvalidResult`): skipped for the rest of the flow, the chunk untouched.

**Reverse listeners** (`reverse`, design 0012) are the other arrival path. `ProxyServer` binds one listener per entry at startup (a bind failure is a `StartupError`, and `/readyz` waits for all of them), and hands each connection to a `ReverseProxySource`. The source runs an `H1Server` with no proxy mode (origin-form requests, no CONNECT, no proxy auth), rewrites each request to the fixed upstream — the authority becomes the upstream's, `X-Forwarded-Host` (if absent) and `X-Forwarded-For` are set — and calls the same `exchange.handle` with an `ExchangeContext` whose `target` is the upstream and whose `direction` is `"inbound"`. So middleware, the access log, Server-Timing, metrics and upstream verification are exactly as above. A change to `reverse` applies on restart; a reload keeps the running listeners.

**Routing through exits** (design 0013, step 1). Each snapshot holds a `Router` built from `exits` and `routing.default_via`. Empty `exits`: the router is off, every route is direct, and `RoutingConnector` connects exactly as `TCPConnector` does. Otherwise `ProxyServer.handle_tunnel` asks the router for the CONNECT's host before anything else, and the answer becomes the `via` of every `Target` the connection makes: the relayed tunnel's (`tunnel=True`, so an HTTP exit is asked to `CONNECT` even without TLS), the decrypted flows' (through `ExchangeContext.fixed_route`), and plain HTTP's (the context's `router`, per request). Reverse listeners take a route for their upstream the same way. The pool keys by the whole `Target`, so a connection through one exit is never handed to another. `RoutingConnector` sends a target with an exit to its kind's connector: `HTTPConnectConnector` opens TCP to the exit (the only name resolved), sends `CONNECT`, reads the answer head with `readuntil` (so no byte of the tunnel is consumed), and then, for TLS, runs `StreamWriter.start_tls` with the same verifying contexts and the origin's SNI; for plain HTTP (`Target.absolute_form`) it skips `CONNECT`, and `H1Upstream`'s `forward_proxy` writes each request in absolute form with the exit's `Proxy-Authorization`. `Socks5Connector` speaks RFC 1928/1929 with the domain-name address type. A refused route never reaches a connector: `_refuse_tunnel` answers the CONNECT through `InterceptedConnection.reject_with` (a `503` with `Retry-After` and `Proxy-Status`), and `exchange.refuse` answers a decrypted or plain request with a local `503`, marking the flow `local` by `routing` before any hook runs. A tunnel already accepted to peek, which turns out not to be decrypted, is closed instead of relayed. Each refusal is logged and counted in `mitm_proxy_route_refused_total`, which `Metrics.enable_routing` registers (with the exit names as `exit` values) only when `exits` is set, so `/metrics` is unchanged without it.

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
- **Chunk-capable hooks from the start, buffered implementation for now;** observe-only middleware stream (design 0012), mutating ones still buffer.
- **Framing from registration, not from what a hook did;** HEAD/1xx/204/304 never framed.
- **Middleware failure fails open,** with the middleware's name in the log; a middleware may opt in to failing closed (`on_error = "fail"`), which ends the flow with `502 middleware <name> failed` in either phase (`Outcome.failed`; designs 0010, 0011). Hooks are checked for `async def` when a middleware is registered (interpose's `require_async`), so a sync hook is a startup or apply error, never a per-flow failure. The hook timeout bounds each hook call and cancels it at its current `await`; a result that comes back after the limit anyway (sync code that overran, or a swallowed cancellation) is a timeout failure (interpose v0.1.4, `Failure.late`).
- **Upstream TLS is always verified;** extra CAs are added, never substituted.
- **Middleware by name from a local registry, an immutable config snapshot, `/v1/status` and `/metrics`:** from [0002: Control-plane management](https://github.com/metalayerlab/mitm-proxy/blob/main/docs/design/0002-control-plane-management.md) (Proposed; only these parts are built).
