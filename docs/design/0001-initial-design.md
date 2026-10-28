# 0001: Initial design

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Approved
Date: 2026-10-28
Authors: Stephen Cox; drafted by Claude (Claude Code)

## Context

mitm-proxy is a lab tool for inspecting, logging and rewriting HTTP(S) traffic in flight. Clients are
configured to use it as their proxy and trust its self-signed root CA. It then terminates TLS for the
hosts it has been told to intercept, runs each request/response through user-written middleware, and
forwards it. Everything else it tunnels without touching it.

Constraints:

- **Load:** hundreds of requests a minute, on a local network. One asyncio process is enough. Throughput
  is not a design driver; correctness on both protocols is.
- **Protocols:** HTTP/1.1 and HTTP/2 from the first release, on both legs.
- **Phase 2 readiness:** transparent interception (iptables/nftables redirect, `SO_ORIGINAL_DST`, SNI
  sniffing) has to be addable later without changing TLS, certificates, the protocol layers or the
  middleware pipeline.
- **Deployment:** the template targets (Docker, Kubernetes, systemd). The template's Dockerfile health
  check and the k8s probes call the admin HTTP API on `listen.port`, so the admin API keeps that port.
- **Security:** this program mints trusted certificates and can read anything it intercepts. It must
  fail closed: an unlisted host is never touched, and the origin's certificate is always
  verified.

## Decision

### Components

One process runs two listeners on one event loop: the **proxy listener** (asyncio `start_server`) and the
**admin API** (the template's FastAPI app, served by `uvicorn.Server.serve()` as a task on the same loop).

```text
src/mitm_proxy/
├── config.py            settings (extended; see Configuration)
├── cli.py               Typer CLI
├── api.py               admin HTTP API (FastAPI)
├── server.py            runs proxy listener + admin API, signal handling, graceful shutdown
├── errors.py            exception types (see Error handling)
├── interception/
│   ├── base.py          InterceptionSource protocol, InterceptedConnection
│   └── explicit.py      ExplicitProxySource: CONNECT and absolute-form request parsing (Phase 1)
├── scope.py             Allowlist: is this host intercepted?
├── classify.py          the single "intercept or relay raw" decision
├── relay.py             raw bidirectional byte relay
├── tls/
│   ├── clienthello.py   ClientHello parser (SNI, ALPN), pure function on bytes
│   ├── ca.py            CertificateAuthority: load/generate the root, mint and cache leaf certs
│   ├── terminate.py     client-facing TLS over ssl.SSLObject + MemoryBIO (buffer-and-replay)
│   └── upstream.py      upstream SSLContext: always verifying, plus an optional extra CA bundle
├── http/
│   ├── flow.py          Flow, Request, Response, Headers, FlowType, ClientConn, ServerConn
│   ├── body.py          BodyStream protocol, BufferedBodyStream, PassthroughBody (internal)
│   ├── normalize.py     header normalization; h2 pseudo-headers <-> h1 request line + Host
│   ├── framing.py       Content-Length vs chunked decision, the HEAD/204/304 exceptions
│   ├── h1.py            HTTP/1.1 client-side server and upstream client, on h11
│   └── h2.py            HTTP/2 client-side server and upstream client, on h2
├── upstream/
│   └── pool.py          ConnectionPool, pooled H1 and H2 upstream connections
├── pipeline.py          Middleware protocol, Action, the pipeline runner
└── middleware/
    └── log.py           FlowLogger: a built-in middleware that logs one line per flow
```

**InterceptionSource.** The seam between "how did this connection get here" and everything else:

```python
@dataclass(frozen=True)
class InterceptedConnection:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    target_host: str          # hostname or IP literal, normalized (see scope.py)
    target_port: int
    prefix: bytes             # client bytes already read past the proxy preamble, replayed first
    client_addr: tuple[str, int]

class InterceptionSource(Protocol):
    def connections(self) -> AsyncIterator[InterceptedConnection]: ...
```

The original requirement was "yield `(client_socket, target_host, target_port)`". Under asyncio, the
socket is the stream pair, and `prefix` carries any bytes read past the CONNECT preamble so that nothing is
lost. `ExplicitProxySource` parses the `CONNECT host:port` line and answers it. Phase 2 adds a
`TransparentSource` (original destination + SNI) and changes nothing downstream. Phase 1 ships only the
protocol and the explicit source. There is no stub file for Phase 2.

`ExplicitProxySource` also accepts absolute-form plain-HTTP requests (`GET http://example.org/ HTTP/1.1`),
the other half of an explicit proxy's job. Those go straight to the HTTP/1.1 layer with no TLS. The same
scope rule applies: an in-scope host goes through middleware, and anything else is forwarded unmodified
with no middleware.

**Proxy authentication.** When `proxy.auth` (`user:password`, secret) is set, `ExplicitProxySource`
requires `Proxy-Authorization: Basic …` on every CONNECT and every plain-HTTP request. It compares the
credentials with `hmac.compare_digest`. A request with no credentials, or the wrong ones, gets
`407 Proxy Authentication Required` with `Proxy-Authenticate: Basic realm="mitm-proxy"`, and the
`Proxy-Authorization` header is never forwarded upstream. When `proxy.auth` is empty there is no
authentication. If `proxy.host` is not a loopback address and `proxy.auth` is empty, `serve` logs a
WARNING at startup ("open proxy"). Basic credentials cross the network in clear text between the client
and the proxy. That is acceptable on a lab LAN, and Security says so.

**Classifier: the single intercept-or-relay decision.** One function decides, and it
is the only place that does:

1. The target host is not in the allowlist → **relay raw**, at once, without peeking.
2. Peek: read until a complete TLS ClientHello has arrived, stopping at `proxy.peek_timeout_ms` or 64 KiB.
   The bytes are kept for replay. Nothing is lost.
3. Not TLS, or the peek timed out (server-speaks-first protocols send nothing) → **relay raw**, replaying
   the peeked bytes.
4. TLS with an SNI that is not in the allowlist (e.g. CONNECT to an allowed host with a different SNI) →
   **relay raw**. No certificate is minted for a host nobody listed.
5. Otherwise → **intercept**: mint a certificate for the SNI (or for the target host when there is no
   SNI), terminate TLS, and hand over to the protocol layer chosen by ALPN.

This is a small change from the original order ("peek, then check scope"). Checking scope first means an
out-of-scope tunnel costs no peek and no latency. The decision is still one function, so TLS
classification and scope stay one decision point, not two code paths.

CONNECT is answered differently depending on the outcome. **Relay:** connect upstream first, then answer
`200`, or `502`/`504` if the connection fails. **Intercept:** answer `200` straight away, and connect
upstream lazily from the pool for each flow.

**Raw relay.** Two copy loops (client→upstream and upstream→client). Each one awaits `drain()` after every
write. When either side half-closes, the shutdown is propagated, and the relay ends when both sides are
done or after `proxy.idle_timeout_ms` with no traffic. No middleware is involved.

**TLS termination.** The ClientHello has already been read off the stream, so the client-facing
handshake runs on `ssl.SSLObject` over `MemoryBIO`s. The peeked bytes are fed in first, and after that the
pump moves bytes between the BIOs and the asyncio stream, awaiting `drain()` on writes. asyncio's
`start_tls()` can't be used here: it ignores bytes already buffered in the `StreamReader`. The terminated
connection is wrapped in the same reader/writer shape the HTTP layers use, so the HTTP layers never know
TLS is there. Client-facing ALPN offers `h2, http/1.1`.

**Certificate authority.**

- The root CA is an ECDSA P-256, self-signed CA certificate (`basicConstraints CA:TRUE, pathlen:0`,
  `keyUsage keyCertSign, cRLSign`) valid for 10 years, made with `cryptography`. There is no OpenSSL CLI.
- Where it comes from, in order: the `ca.cert` + `ca.key` settings (for Kubernetes secrets), then
  `<state_dir>/ca/ca.pem` + `ca-key.pem`. If neither exists, it is generated on the first `serve` or by
  `ca init`. The key file is written with mode `0600`, and its SHA-256 fingerprint (never the key) is
  logged.
- Leaf certificates: a single ECDSA P-256 leaf key pair is generated at startup and shared by every leaf,
  so minting a leaf is one signature. That is what keeps it at about a millisecond, synchronous and
  in-process. (Generating a fresh RSA key per host would take tens of milliseconds.) Each leaf has a SAN of
  exactly the host (a DNS name, or an IP SAN for IP literals) and never a wildcard. It is valid for 30 days
  and backdated 1 day for clock skew. Leaves are cached in memory in an LRU of `ca.leaf_cache_size`,
  keyed by the normalized hostname alone. The key does not depend on the protocol, so h1 and h2 share
  a cert.
- There is no async or HSM signing path (deferred).

**Upstream TLS.** `ssl.create_default_context()`: the system trust store, with
`check_hostname` on. If `upstream.extra_ca_file` is set, its CAs are **added** to the store for internal
lab targets. They never replace it. There is no setting, flag or code path that disables verification.
Upstream ALPN offers `h2, http/1.1`, or only `http/1.1` for a WebSocket upgrade.

**Data model: Flow.** A Flow is one logical exchange: one h2 stream, or one h1 request.

```python
class FlowType(Enum): HTTP, WEBSOCKET          # RAW_TCP reserved for Phase 2

@dataclass
class Flow:
    id: str                     # unique per exchange (uuid4 hex)
    type: FlowType
    protocol: str               # client-leg ALPN result: "http/1.1" | "h2"; read-only (property)
    client_conn: ClientConn     # peer addr, offered SNI, offered ALPN, negotiated TLS version/cipher
    server_conn: ServerConn | None   # set once upstream is chosen; may be SHARED across flows (pool)
    request: Request
    response: Response | None   # None until upstream answers, or a middleware sets it
    metadata: dict[str, Any]    # for middleware's own state between its hooks
    # shortcuts: flow.host, flow.port, flow.method (read through to request)

@dataclass
class Request:  method, scheme, authority, path, headers: Headers, body: BodyStream,
                trailers: Headers | None
@dataclass
class Response: status, reason, headers: Headers, body: BodyStream, trailers: Headers | None
```

- `Headers` is an ordered, case-insensitive multimap with lower-case names. **Normalization** (both
  protocols, both directions) takes out the connection-specific headers RFC 9113 §8.2.2 forbids in h2:
  `Connection` and the headers it names, `Keep-Alive`, `Proxy-Connection`, `Transfer-Encoding`, `Upgrade`,
  and `TE` other than `trailers`. h1's `Host` becomes `authority`, and h2's pseudo-headers become the
  `method/scheme/authority/path` fields, so middleware never sees a pseudo-header. Framing headers are
  written again on the way out (see Body framing).
- `protocol` is the **client** leg. The upstream leg's protocol is on `server_conn.protocol`. These can
  differ (h2 client, h1-only origin), and the h1/h2 layers translate between them.
- Connection-level h2 state (HPACK, SETTINGS, flow-control windows) stays in the `h2.py` connection
  objects. A Flow never holds it.
- Serialization: Flow's fields are plain data (no sockets or loop objects outside `body`), so that a
  serializer can be added later without changing Flow's shape. Phase 1 has no serializer and commits to no
  format.

**Body handling.**

```python
class BodyStream(Protocol):
    def __aiter__(self) -> AsyncIterator[bytes]: ...
    async def read_all(self) -> bytes: ...
    def is_complete(self) -> bool: ...
```

- Bodies are de-framed (no chunk-size lines, no DATA-frame boundaries) but **not** content-decoded:
  gzip stays gzip.
- `BufferedBodyStream` (Phase 1) reads the whole body before middleware sees it, and `__aiter__` yields
  it as exactly one chunk.
- **Oversized bodies are passed through, not refused.** A buffered body may be at most
  `body.max_buffer_bytes`. When a message goes over that limit, that message's hooks are skipped (a
  request: every hook of the flow; a response: `on_response` and the response chunk hooks). The bytes
  already buffered and the rest are streamed through with their original framing. Headers haven't been
  sent at that point, so nothing has been committed. No hook ever sees part of the body. The flow is not
  inspected, so the gap is made visible: a WARNING with the flow ID and the size, and an
  `oversize_passthrough` counter in `/stats`. This is the same "degrade to not applied, never break the
  traffic" rule as for middleware failure.
- Fast path: a flow where no registered middleware applies is never buffered. Its body is passed through
  chunk by chunk (with backpressure) and its `Content-Length` is left as it was.
- **Any middleware that applies forces buffering**, whether or not it has chunk hooks, because any hook
  may call `read_all()`. A middleware that applies to every flow (the default `applies_to`) therefore
  buffers every intercepted flow. `applies_to` is the way to narrow it, and Writing-Middleware says so.
- The proxy sends `100 Continue` itself to a client that asks for it when it is about to buffer that
  client's request body.

**Middleware.**

```python
class Action(Enum): CONTINUE, STOP, DENY

class Middleware:                         # base class; every hook optional
    def applies_to(self, flow: Flow) -> bool: return True   # called once, on request headers
    async def on_request(self, flow: Flow) -> Action: ...
    async def on_request_chunk(self, flow: Flow, chunk: bytes, is_last: bool) -> bytes | None: ...
    async def on_response(self, flow: Flow) -> Action: ...
    async def on_response_chunk(self, flow: Flow, chunk: bytes, is_last: bool) -> bytes | None: ...

pipeline.register(name: str, middleware: Middleware)   # name required, unique, used in logs
```

- **Order** is registration order, a plain list. There are no priorities and no DAG.
- **Chunk hooks** return `None` to pass the chunk through, or `bytes` to replace it. These signatures are
  fixed now, even though Phase 1 calls each chunk hook once per body.
- **Actions.** `CONTINUE` moves to the next middleware. `STOP` ends the pipeline for that phase, and the
  flow stands as it is. A `STOP` in `on_request` with `flow.response` set sends that response and never
  contacts upstream (e.g. a cache hit). A `STOP` in `on_request` without a response is a middleware bug:
  it is logged and answered `502`. `DENY` in `on_request` gets `403`, and upstream is not contacted. `DENY`
  in `on_response` replaces the response with `502`. (Response headers haven't been sent at that point.)
  Both carry a plain-text body, `denied by middleware <name>`, so that a denial can't be mistaken for an
  origin or proxy failure. Resetting the stream instead was rejected: the client would get an opaque
  error.
- **Mutation-capable** means the middleware overrides a chunk hook (checked with the class, at
  registration time). A flow is mutation-capable if any middleware whose `applies_to(flow)` is true is
  mutation-capable.
- **Runner rule.** Every hook is `await`ed in order, inside the read/write loop. The runner
  never wraps a hook in `create_task()`. That keeps backpressure end to end: a slow hook slows the read
  loop and nothing piles up ahead of it. It is a property of `pipeline.py`'s structure, it is covered by a
  test, and it is called out in the module docstring for reviewers.
- **Failure.** Every hook runs under `asyncio.timeout(middleware.hook_timeout_ms)`. If a hook
  raises or times out: (1) log at ERROR with the middleware's name, the flow ID and the exception;
  (2) mark that middleware *failed for this flow*, so its remaining hooks are skipped and treated as
  `CONTINUE`/`None`; (3) carry on. The connection and the process survive. Limitation: a hook that blocks
  the event loop (synchronous CPU work or blocking I/O) can't be timed out and stalls the proxy. This goes
  in Troubleshooting.
- **Loading.** (Changed by [0002](0002-control-plane-management.md): `middleware.chain` holds names from a
  local `middleware.available` registry.) Middleware comes from the `middleware.chain` setting, as `name=module:attribute` entries
  in order, where `attribute` is a `Middleware` subclass (instantiated with no arguments) or an instance.
  Middleware can also be registered in Python when embedding (`Proxy(config, middleware=[...])`). The
  middleware API (`mitm_proxy.pipeline`, `mitm_proxy.http.flow`) is a public interface and is documented
  on its own wiki page.
- **Built-in:** `FlowLogger` (`log=mitm_proxy.middleware.log:FlowLogger`) logs one INFO line per flow:
  flow ID, protocol on each leg, method, authority, path, status, body sizes and duration. It is the
  reference example. It is **not** in the default chain, because it applies to every flow and so would
  buffer every intercepted flow (see Body handling). Quick-Start shows how to turn it on. It does not
  override chunk hooks, so it doesn't force chunked framing.

**Body framing.** The framing is decided once per message, from what is registered, never from
what a hook actually did:

- The flow is mutation-capable and the message has a body → h1: remove `Content-Length`, send
  `Transfer-Encoding: chunked`. h2: send no `content-length` and let the DATA frames carry the body.
- Not mutation-capable → `Content-Length` passes through as it was.
- **Never** add body-framing headers to a response to `HEAD`, a `1xx`, `204` or `304`, whatever
  middleware is registered. This is a separate branch in `framing.py` with its own tests, not something
  the general rule happens to produce.
- "Has a body" for a request means a declared `Content-Length > 0` or chunked (h1), or no `END_STREAM`
  on HEADERS (h2). Chunk hooks are not called on a request with no body.
- Trailers are forwarded when the outgoing leg can carry them (h2, or h1 chunked to a client that sent
  `TE: trailers`). Otherwise they are dropped and a DEBUG line is logged.

**WebSocket.** An h1 request with `Upgrade: websocket` becomes a `FlowType.WEBSOCKET` flow.
**No hooks run** (that includes the upgrade request). It is forwarded on a dedicated, unpooled
HTTP/1.1-only upstream connection. On `101` both sides are handed to the raw relay. Anything else is
answered as an ordinary response and the connection carries on. The proxy does not advertise
`SETTINGS_ENABLE_CONNECT_PROTOCOL`, so clients don't attempt WebSocket over h2 (RFC 8441), and browsers
fall back to an h1 connection for it.

**Upstream pool.** The pool is keyed by `(host, port, sni)`, where `sni` is what the flow's
client sent. It is safe for concurrent use from many flow coroutines: there is one `asyncio.Lock` per
key, held only while a connection is checked out or created.

- **h2 upstream:** one connection per key. Flows multiplex onto it (the `h2` library allocates stream IDs)
  up to the server's `SETTINGS_MAX_CONCURRENT_STREAMS`. Past that, a second connection is opened, still
  within the per-host limit. When `GOAWAY` arrives, the connection is drained (open streams finish, no new
  ones start) and replaced.
- **h1 upstream:** at most `upstream.max_connections_per_host` connections, the idle ones reused
  most-recently-used first. If they are all busy, the flow waits for one, and the wait counts towards
  `upstream.connect_timeout_ms`.
- **Lifecycle:** an idle connection is closed after `upstream.idle_timeout_ms`. There is no active probing.
  A connection is health-checked when it is checked out (transport closing, or EOF/data waiting on an idle
  h1 socket → discard). If a *reused* h1 connection fails before any response byte arrives, an idempotent
  request (GET, HEAD, OPTIONS, PUT, DELETE) is retried once on a fresh connection. Nothing else is
  retried.
- When the pool is shut down, every connection gets `GOAWAY` (h2) or is closed (h1).

### Data flow

```text
client ──TCP──▶ proxy listener
                 │
                 ▼
     InterceptionSource (Phase 1: ExplicitProxySource)
       CONNECT host:port ─────────┐            absolute-form GET http://… ──▶ h1 layer (no TLS) ─┐
                                  ▼                                                               │
                  classify(target_host, peeked ClientHello)                                       │
          ┌────────── relay ──────┴────── intercept ──────────┐                                   │
          ▼                                                   ▼                                   │
   raw byte relay ◀──── upstream          CA: mint/cache leaf for SNI                             │
   (no TLS, no middleware)                TLS terminate (MemoryBIO, replay peeked bytes)          │
                                          ALPN → h1.py (h11) | h2.py (h2)                         │
                                                         │  one Flow per request / per stream    │
                                                         ▼◀────────────────────────────────────────┘
                                         Upgrade: websocket? ── yes ──▶ dedicated h1 upstream → raw relay
                                                         │ no
                                                         ▼
                                normalize headers → pipeline.on_request (in order, awaited)
                                   → on_request_chunk(s) → framing decision → upstream pool
                                   → (h1|h2, verified TLS) → origin
                                origin response → normalize → on_response → on_response_chunk(s)
                                   → framing decision (HEAD/1xx/204/304 exception) → client leg
```

### Configuration

All settings go through `config.SETTINGS`, with the usual precedence (`_FILE` > env > YAML > default).

To support lists, `config.py` grows a list kind. A list default is `""` in `SETTINGS`, so the generated
wiki table still reads *(none)* without a change to the template's script. A list can be a YAML list, or a
comma-separated string in an environment variable (neither hostnames nor import paths can contain a
comma), and `Config` holds it as `tuple[str, ...]`. Times are integers in milliseconds (`*_ms`), because
`Value` has no float and the peek timeout needs better than one-second resolution.

| Key | Type | Default | Secret | Meaning |
|---|---|---|---|---|
| `log_level` | str | `INFO` | | Existing. |
| `listen.host` | str | `127.0.0.1` | | Existing, default changed from `0.0.0.0`: admin API bind address. Container manifests set `0.0.0.0`. |
| `listen.port` | int | `8080` | | Existing: admin API port; Dockerfile health check and k8s probes use it. |
| `state_dir` | str | `/var/lib/mitm-proxy` | | Existing; the CA lives in `<state_dir>/ca/`. |
| `proxy.host` | str | `127.0.0.1` | | Proxy listener bind address. Loopback by default: an exposed proxy is an open proxy. |
| `proxy.port` | int | `3128` | | Proxy listener port. |
| `proxy.auth` | str | *(none)* | **yes** | `user:password` for Basic proxy authentication, via `MITM_PROXY_PROXY_AUTH_FILE`. Empty = no authentication. |
| `proxy.peek_timeout_ms` | int | `1000` | | How long to wait for a ClientHello before relaying raw. |
| `proxy.idle_timeout_ms` | int | `120000` | | Close a client connection or relay that has been idle this long. |
| `proxy.shutdown_grace_ms` | int | `10000` | | On SIGTERM, how long in-flight flows get to finish. |
| `intercept.hosts` | list | *(none)* | | The allowlist. Empty = intercept nothing (a pure tunnelling proxy). |
| `middleware.chain` | list | *(none)* | | Ordered `name=module:attribute` entries. |
| `middleware.hook_timeout_ms` | int | `5000` | | A hook that runs longer has failed. |
| `body.max_buffer_bytes` | int | `67108864` | | The largest body buffered for middleware (64 MiB). Larger bodies skip middleware and stream through. |
| `ca.cert` | str | *(none)* | | Root CA certificate, PEM (use `MITM_PROXY_CA_CERT_FILE`). Empty = `<state_dir>/ca/ca.pem`. |
| `ca.key` | str | *(none)* | **yes** | Root CA private key, PEM, via `MITM_PROXY_CA_KEY_FILE`. Must be set with `ca.cert`, or not at all. |
| `ca.common_name` | str | `mitm-proxy CA` | | The CN of a newly generated root, so a client's trust store shows which proxy it is. |
| `ca.leaf_cache_size` | int | `1024` | | Leaf certificates kept in memory. |
| `upstream.extra_ca_file` | str | *(none)* | | A PEM bundle of extra CAs trusted for origins, on top of the system store. |
| `upstream.connect_timeout_ms` | int | `10000` | | TCP + TLS connect to an origin, including the wait for a pool slot. |
| `upstream.idle_timeout_ms` | int | `60000` | | Close a pooled upstream connection that has been idle this long. |
| `upstream.max_connections_per_host` | int | `6` | | The pool's limit for each `(host, port, sni)`. |

Validation (`ConfigError`): ports in range; timeouts and sizes > 0; every `intercept.hosts` entry is a
valid exact host, IP literal or `*.suffix` pattern; every `middleware.chain` entry is `name=module:attr`
and the names are unique (imports are checked when `serve` starts, not by `config`); `ca.cert` and
`ca.key` are both set or both empty; `proxy.auth`, when set, contains a `:` and a non-empty user.

**Allowlist syntax.** The candidates were exact match, wildcard suffix and regex. The decision is exact
names plus a wildcard suffix, and no regex.

- `example.org` matches exactly that host.
- `*.example.org` matches any subdomain at any depth (`a.example.org`, `a.b.example.org`) but **not**
  `example.org` itself. To get both, list both.
- Matching ignores case. Names are IDNA-encoded and a trailing dot is stripped before matching. IP
  literals match exactly (IPv6 in normalized form).
- Ports are not part of the match: a listed host is intercepted on every port.
- A lone `*` is rejected. Intercepting everything is the rejected blanket mode.

### CLI commands

| Command | What it does | Exit codes |
|---|---|---|
| `mitm-proxy version` | Existing. | 0 |
| `mitm-proxy config [-c FILE]` | Existing. It now covers the new settings, and `ca.key` and `proxy.auth` are masked. | 0, 2 |
| `mitm-proxy serve [-c FILE]` | Runs the proxy listener and the admin API. Loads the CA, generating it if it is missing, and imports the middleware chain. | 0 on a clean shutdown, 2 on a startup error |
| `mitm-proxy ca init [-c FILE] [--force]` | Creates the root CA in `<state_dir>/ca/`. Refuses to overwrite one without `--force`. Prints the fingerprint. | 0, 2 |
| `mitm-proxy ca show [-c FILE] [--der]` | Writes the root certificate (PEM, or DER with `--der`) to stdout, to install on clients. | 0, 2 |
| `mitm-proxy scope check HOST [-c FILE]` | Says whether HOST would be intercepted, and which allowlist entry matched. | 0 = intercepted, 1 = tunnelled, 2 = error |

### HTTP endpoints (admin API)

> Replaced by [0002](0002-control-plane-management.md), except `/healthz` and `/readyz`.

The admin API is read-only. It has no authentication, because nothing on it changes state or reveals a
secret. The root certificate it serves is public by design.

| Method & path | Response |
|---|---|
| `GET /healthz` | Existing: `{"status": "ok"}`, with no dependencies checked. |
| `GET /readyz` | `200 {"status": "ready"}` once the proxy listener is bound and the CA is loaded. `503 {"status": "starting"\|"stopping"}` otherwise. |
| `GET /version` | Existing. |
| `GET /ca.pem` | The root certificate, `application/x-pem-file`. |
| `GET /ca.der` | The same, DER, `application/x-x509-ca-cert`. Phones offer to install this content type when they download it. |
| `GET /stats` | Counters since start: active client connections; flows by type (intercepted HTTP, WebSocket, relayed tunnels, plain HTTP); upstream pool sizes by protocol; middleware failures by middleware name; `oversize_passthrough`; proxy authentication failures. No hostnames. |

### Error handling

Exceptions (`errors.py`), all specific:

- `ConfigError` (existing): an invalid setting.
- `CAError`: CA files that can't be read, are malformed, or don't match (the key isn't the certificate's
  key), or a key file with permissions looser than `0600` (a WARNING, not a failure).
- `StartupError`: the listener can't bind, or a middleware entry won't import or isn't a `Middleware`.
- `ProtocolError`: a malformed request or frame from the client.
- `UpstreamError` (subtypes `UpstreamConnectError`, `UpstreamTLSError`, `UpstreamTimeout`): an origin
  that couldn't be reached or failed verification.

On the CLI, `ConfigError`, `CAError` and `StartupError` print one line, `error: …`, to stderr and exit `2`.

At run time nothing escapes a connection's task. Each connection handler catches its expected exceptions
and logs anything unexpected with a traceback and the flow ID, and the process keeps running.

| Situation | Client sees | Log |
|---|---|---|
| Malformed CONNECT or request line | `400`, then the connection is closed | WARNING |
| Missing or wrong proxy credentials (`proxy.auth` set) | `407` with `Proxy-Authenticate` | INFO, client address (never the credentials) |
| CONNECT relay: upstream connect fails or times out | `502` / `504` | INFO |
| Intercepted flow: connect fails | `502` with a one-line plain-text reason | WARNING, flow ID |
| Intercepted flow: **upstream certificate fails verification** | `502`, body names the verification error | WARNING, flow ID, host |
| Intercepted flow: upstream timeout | `504` | WARNING |
| h2 stream protocol error | `RST_STREAM` on that stream only; the connection survives | WARNING |
| h2 connection protocol error | `GOAWAY`, connection closed | WARNING |
| Client rejects our leaf (CA not installed) | TLS alert from the client; nothing to send | INFO, with a hint to install the CA |
| Middleware raises or times out | Flow continues without that middleware | ERROR: middleware name, flow ID |
| `DENY` | `403` (request) / `502` (response), body `denied by middleware <name>` | INFO, middleware name |
| Body over `body.max_buffer_bytes` | The message as sent by its origin; middleware skipped | WARNING: flow ID, size |

Error responses go to the client on the client's own protocol, and they are never forwarded upstream.

Shutdown: SIGTERM/SIGINT → `/readyz` answers 503, stop accepting connections, h2 clients get `GOAWAY`,
in-flight flows get `proxy.shutdown_grace_ms`, then everything still open is closed.

### Dependencies

These are new; every one is a direct dependency, and every one lives up to the "only when needed" rule:

- `h2` (python-hyper): HTTP/2 state machine, I/O-agnostic. Writing HTTP/2 ourselves was rejected.
- `h11` (python-hyper): an HTTP/1.1 state machine, I/O-agnostic, the h1 counterpart to `h2`. It is
  already installed (uvicorn depends on it) and becomes a direct dependency, so that we don't lean on
  someone else's transitive one. Writing our own HTTP/1.1 parser (chunked encoding, pipelining,
  `Upgrade`) would be wasted effort and a correctness risk.
- `cryptography`: CA and leaf generation (no shelling out to the OpenSSL CLI).

The ClientHello parser is stdlib code (it is only a few dozen lines: record header, handshake type 1, the
SNI and ALPN extensions), and so is everything else.

### Explicitly out of scope (Phase 1)

- Transparent interception: Phase 2. Only the `InterceptionSource` seam exists.
- `RAW_TCP` flows and any middleware on non-HTTP or relayed traffic.
- True chunk-by-chunk streaming to middleware (`StreamingBodyStream`). The interface is ready for it,
  but the implementation buffers.
- WebSocket message inspection or modification. WebSocket over HTTP/2 (RFC 8441).
- HTTP/3 / QUIC. Clients that use QUIC go around an HTTP proxy, and Troubleshooting will say how to
  turn that off.
- h2c (cleartext HTTP/2, prior knowledge or `Upgrade: h2c`) on either leg.
- Content decoding (gzip/br/zstd) of bodies for middleware.
- Storing flows, replaying them, or a flow viewer UI. No serializer.
- HSM-backed or async certificate signing.
- Authentication to the admin API. Proxy authentication beyond one Basic `user:password` (Digest, NTLM,
  several users).
- Per-middleware options in config (a YAML mapping form for `middleware.chain`). It can be added later
  without breaking the `name=module:attribute` form.
- Upstream proxies (chaining), SOCKS, client certificates (mTLS) on either leg, certificate revocation
  checks (OCSP/CRL) beyond what the system TLS stack does.
- Hot reload of the allowlist or middleware: restart to apply.
- Multi-process or multi-host scaling.

## Alternatives considered

These were decided before this record was written, and are carried over:

- **Both interception modes now, or none of the seam.** Rejected. Building transparent mode
  first means debugging two unknowns at once. Leaving the seam out and adding it later lets
  "there is always a CONNECT line" leak into TLS and the pipeline.
- **HTTP/1.1 only at first; requiring the same protocol on both legs; writing HTTP/2
  ourselves.** Rejected. A data model built for h1 doesn't stretch to multiplexed streams. Insisting on
  matching protocols would refuse valid traffic. HTTP/2's state machine is intricate and a library already
  gets it right.
- **Assuming every CONNECT is HTTPS; a separate sniffing pass; declaring protocols per host.**
  Rejected. The first hangs on non-TLS tunnels. The second repeats the ClientHello parse. The third is
  fragile configuration.
- **Raw frames or bytes for middleware; one Flow per connection; host and method only on a nested
  object.** Rejected. They break bridging between protocols, break h2 multiplexing, and make
  everyday predicates awkward to write.
- **A buffered-only hook signature (`on_response(flow, full_body)`).** Rejected. It can't be
  upgraded to streaming without rewriting every middleware.
- **Spotting mutation at run time and re-buffering; always using chunked.** Rejected. The
  first quietly falls back to full buffering under load. The second adds overhead where nothing can
  mutate.
- **Our own backpressure or windowing layer.** Rejected: `drain()` and h2 flow control already
  do it.
- **Ordering middleware by priority number or DAG.** Rejected. Priorities produce ordering
  dependencies nobody can debug, and a DAG is too much for a handful of middleware.
- **Killing the flow when middleware fails; retrying the middleware.** Rejected. Killing is too
  disruptive while middleware is being developed. A retry is undefined after side effects, and it doesn't
  fix a hang. (In a security-enforcement setting, deny-on-error might be better. That is noted, not
  pursued.)
- **Intercepting everything; a denylist.** Rejected. Both fail open, both break apps that pin
  certificates, and both mint certificates nobody needs.
- **A switch to turn off upstream verification.** Rejected. A MITM proxy that doesn't verify
  the origin is a silent, total security downgrade. Internal CAs are what `upstream.extra_ca_file` is for.
- **A fresh upstream connection per flow.** Rejected: a TLS handshake on every request, and
  nothing like how real clients behave.

New in this record:

- **Regex allowlist entries.** Rejected for Phase 1. A regex is easy to get subtly wrong (unanchored, `.`
  unescaped) in exactly the place where getting it wrong intercepts the wrong host. Exact names plus
  `*.suffix` cover the lab cases.
- **`MSG_PEEK` on the raw socket, then `connect_accepted_socket(ssl=…)`.** Rejected. It would need our
  own accept loop and byte-exact reading of the CONNECT preamble off a raw socket. Buffer-and-replay over
  `MemoryBIO` works on any stream, and the TLS path can be unit-tested in memory.
- **Connecting upstream before the client handshake, to copy the origin's SANs and ALPN.** Rejected. It
  ties the two legs together, when they are meant to be negotiated independently. It also adds a round trip
  before the client's handshake can finish. A SAN of exactly the requested host is enough for any client
  that connects by name.
- **One RSA key per leaf.** Rejected: tens of milliseconds per host, which breaks the "synchronous,
  in-process" budget for no lab benefit.
- **A separate admin process or port model.** Rejected. One process, two listeners, and the template's
  health checks keep working unchanged.
- **Proxy on `8080`, admin API moved.** Rejected. It would mean changing the template-owned Dockerfile
  health check and probes for the sake of a habit. The proxy takes `3128`.
- **`*.example.org` matching one label only, like TLS wildcards.** Rejected. In a lab, "everything under
  example.org" is what people mean.
- **Refusing oversized bodies (`413`/`502`).** Rejected. It breaks large downloads from allowlisted hosts.
  Passing them through uninspected, loudly logged, matches how middleware failure is handled.
- **Resetting the stream on a response `DENY`.** Rejected. The client gets an opaque error, which is
  harder to debug than a `502` that names the middleware.
- **`FlowLogger` on by default.** Rejected. It applies to every flow, so it would turn off the unbuffered
  fast path for all intercepted traffic.
- **No proxy authentication in Phase 1.** Rejected. The template ships Kubernetes manifests, so the
  proxy won't stay on loopback, and without authentication it is an open proxy into the network.

## Consequences

- **Easier:** Phase 2 becomes one new `InterceptionSource` plus OS redirect rules. Middleware is written
  once against `Flow` and works whichever protocols the two legs end up on. Turning on streaming later
  means swapping in a new `BodyStream` implementation, with no change to the middleware API.
- **Harder or costly:**
  - We own a TLS pump (MemoryBIO) and an h1↔h2 translation layer. That is where most of the bugs will be,
    and it needs the most tests: in-memory loopback tests for both protocol pairings, and the HEAD/204/304
    branch tested on its own.
  - Forcing chunked framing on a mutable flow means an h1 origin that rejects chunked request bodies
    (`411`) will fail when mutation-capable middleware applies. Using `applies_to` to narrow the middleware
    is how to avoid it, and Troubleshooting will say so.
  - Middleware that blocks the event loop can't be timed out (see Middleware).
  - The CA key is the crown jewels: whoever holds it can intercept any client that trusts it. Security
    has to say so plainly, and the k8s manifests have to mount it as a Secret.
- **Interface changes:** new settings, the list kind in `config.py`, CLI groups `ca` and `scope`, and new
  admin routes. The proxy port (`3128`) is new and needs exposing in `contrib/` (compose port, k8s
  Service port and NetworkPolicy). The example config gains the new keys. `listen.host` now defaults to
  `127.0.0.1`, so the compose file and the k8s config must set `listen.host` and `proxy.host` to `0.0.0.0`
  explicitly. Otherwise published ports and probes can't reach the process. The k8s manifests should also
  set `proxy.auth` from a Secret. The Dockerfile health check calls `127.0.0.1` inside the container and
  keeps working.
- **Inspection gaps are possible:** an oversized body passes through uninspected. That is visible in the
  logs and in `/stats`, but no middleware is told about it.
- **Rollout and rollback:** this is the first release, so there is nothing to migrate. Rolling back means
  going back to the skeleton. Root CAs generated by the first release stay valid, because the file layout
  in `<state_dir>/ca/` is fixed from now on.
- **Wiki pages that change:** Introduction, Quick-Start (install the CA, point a client at the proxy),
  In-Depth-Configuration (allowlist syntax, CA sources), CLI-Reference (regenerate), HTTP-API-Reference
  (regenerate and document the new routes), Architecture-Internals (components, data flow, the decisions
  above), Security (threat model: the CA key, an open proxy, loopback defaults, `proxy.auth` and Basic's
  clear-text credentials, mandatory upstream verification), Troubleshooting (CA not trusted, QUIC bypass, `411` with chunked, blocking middleware,
  verification failures against internal CAs, `407` from a client without credentials, oversized bodies skipping middleware), Testing-Guide (in-memory protocol tests), Deployment (the
  proxy port, the CA secret), FAQ. A new **Writing-Middleware** page (the public middleware API), linked
  from `_Sidebar.md`.
- **TODO.md:** the rejected alternatives above go into "Decided against".

## Open questions

None. Resolved in review, 2026-10-28:

1. Allowlist: exact names plus `*.suffix` matching any depth, the apex not included, no regex.
2. Pool defaults as proposed: 6 per host, 60 s idle, health checked at checkout, one retry for idempotent
   requests on a reused h1 connection (never for POST).
3. Flow is plain data, with no serializer or format in Phase 1.
4. Proxy on `3128`, admin API stays on `8080`.
5. Optional Basic proxy authentication (`proxy.auth`) is in Phase 1.
6. `listen.host` defaults to `127.0.0.1`, and the container manifests set `0.0.0.0`.
7. Plain-HTTP (absolute-form) requests are in Phase 1.
8. Oversized bodies skip middleware and stream through, with a WARNING and a `/stats` counter.
9. A response `DENY` is a `502` with a body naming the middleware.
10. `FlowLogger` is off by default.
11. `name=module:attribute` strings only. A richer YAML form can come later.
