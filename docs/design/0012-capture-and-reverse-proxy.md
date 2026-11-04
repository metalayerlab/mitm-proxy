# 0012: Capture middleware and reverse-proxy mode

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Implemented (v0.3.2 to v0.3.6; approved by the brain coordinator for Stephen, 2026-11-03)
Date: 2026-11-03
Authors: Stephen Cox; drafted by Claude (Claude Code)

## Context

The use case: record what an API service is asked and what it answers (its **inbound** traffic), and
the calls it makes to an LLM API while answering (its **egress** traffic), as files someone can read
and compare later. The service runs in a sealed network, where the proxy is the only way in
and the only way out. Five things stand in the way today:

1. **Only forward mode.** Every client has to be configured to use the proxy (0001). The service's own
   clients can't be: they connect to an address, and in a sealed network that address has to be the
   proxy's.
2. **No per-middleware options.** A middleware is constructed with no arguments (0001), and 0001 left a
   YAML form for options to later. A capture middleware needs a directory, rules and redaction settings.
3. **Storing flows is out of scope** (0001's "Explicitly out of scope"). Every user would write the same
   recorder, with the same redaction mistakes.
4. **Every applying middleware buffers** (0001). An LLM API streams its answer as server-sent events; a
   recorder that buffers holds every token back until the last, and its recorded timings describe the
   proxy, not the origin.
5. **One timeout for both sides.** The pool's read timeout is `proxy.idle_timeout_ms` (120 s), set in
   `server.py`. An LLM call can be silent for longer than a client connection should be allowed to idle.

Stephen approved the five decisions below on 2026-11-03. This record writes them down, with the details
that follow from them; the brain coordinator approved it for Stephen on 2026-11-03, with the questions it
raised decided as below.

Constraints:

- **Fail open** (0001, 0010): a capture that can't write never breaks the traffic it records.
- **No new dependency.** JSON, base64, gzip and the path matching are stdlib.
- **Nothing written before redaction.** Credentials must not reach disk, not even briefly.
- The middleware API stays compatible: existing middleware needs no change.

## Decision

### 1. Reverse-proxy mode

A new interception source, `ReverseProxySource`, beside `ExplicitProxySource`. It serves plain HTTP/1.1
on its own listener and sends every request to one fixed upstream.

```yaml
reverse:
  - listen: 0.0.0.0:8081
    upstream: http://api.internal:8000
  - listen: 127.0.0.1:8082
    upstream: https://llm-gateway.internal
```

- **`reverse`** (a list of mappings; default empty: no reverse listeners). `listen` is `host:port`
  (`[::1]:8081` for IPv6). `upstream` is `http://` or `https://`, a host and an optional port, with no
  path, query or user info. Listen addresses are unique. Invalid entries are a `ConfigError`.
- **Same path as the forward proxy.** Each accepted connection gets an `ExchangeContext` with a fixed
  `target` (the upstream's host, port and TLS; SNI is the upstream host), the current snapshot's pipeline,
  and the same pool, so middleware, the access log, Server-Timing, metrics and upstream verification
  (always on, 0001) work unchanged. WebSocket upgrades take the existing dedicated-connection path.
- **The request.** `flow.request.authority` becomes the upstream's authority, so middleware, the
  access log and the origin all see the name the request goes to. The client's `Host` is kept in
  `X-Forwarded-Host` (set only when absent), and the client's address is appended to `X-Forwarded-For`.
  Nothing else is added: no `Forwarded` (RFC 7239) until someone needs it, and no `X-Forwarded-Proto`,
  which a TLS terminator in front sets itself.
- **Scope.** Every flow on a reverse listener is intercepted: the operator listed the upstream by
  configuring the listener, so `intercept.hosts` doesn't apply. Middleware still selects with
  `applies_to`.
- **No proxy authentication.** Clients of a reverse proxy don't send `Proxy-Authorization`, and the
  listener only reaches its one upstream. `proxy.auth` doesn't apply; the listener's exposure is the bind
  address, as for any service.
- **Direction.** `Flow` gains a read-only `direction`: `"inbound"` for a reverse listener's flows,
  `"egress"` for the forward proxy's (CONNECT and plain HTTP). It is a property of the source, set when
  the flow is built.
- **Lifecycle.** Listeners are bound at startup, like `proxy.port` (bootstrap settings in 0002's terms);
  one that can't bind is a `StartupError`. `/readyz` is ready once every listener is bound. A reload
  that changes `reverse` logs a WARNING that the change applies on restart.
- **Later:** TLS on the reverse listener, and HTTP/2 on it (h2c is out of scope, 0001). Until then a TLS
  terminator (Caddy, for example) sits in front and forwards plain HTTP.

### 2. Per-middleware configuration

```yaml
middleware:
  chain: [capture]
  config:
    capture: { ... }   # free-form: whatever this middleware accepts
```

- **`middleware.config.<name>`** is a mapping, passed to that middleware's constructor as one positional
  argument, a read-only mapping (nested mappings and lists frozen too). With no entry for a name, the class
  is constructed with no arguments, as today. A class that rejects the argument is a `StartupError`
  naming the middleware, as is an entry for a middleware that `middleware.available` names as an instance
  (there is nothing to construct). An entry for a name that isn't available (built in or in
  `middleware.available`) is a `ConfigError`. The middleware validates its own mapping in its
  constructor; an invalid mapping is a `StartupError` at startup and a refused reload (0011's rule).
- **Reload.** Middleware is constructed again on every reload (`_build`), so a changed mapping applies to
  flows that start after the reload.
- **How it fits interpose's Settings.** interpose declares every setting up front, and a value is a
  scalar, a list of scalars or a `Secret`; nested objects are a non-goal (its Introduction). A YAML key
  that isn't declared is ignored. Neither `middleware.config` nor `reverse` can be declared today.
  Sections live in interpose, not in a second YAML reader here (which would duplicate interpose's file
  handling and could disagree with it; sluice will want the same thing). That is an interpose change,
  under its own interpose record: a declared **section** setting,
  whose value is the deep-frozen YAML subtree at its key. It comes from the YAML file, or from
  `<PREFIX><KEY>_FILE` (a YAML file with the whole section, for a Kubernetes ConfigMap or Secret mount),
  never from a plain environment variable (a mapping in a variable is unreadable). It keeps the lookup
  order and the "values are never logged" rule, and it gives `scripts/gen-wiki.py` a row to generate.
  mitm-proxy declares `middleware.config` and `reverse` as sections and validates them in `config.py`.
- **Managed (0002).** `middleware.config` is a managed setting, like `middleware.chain`. Because it can
  name a directory the proxy writes to, when managed mode is built `capture.dir` must be under a new
  bootstrap setting, `capture.root`; a `dir` outside it is a refused configuration. Until managed mode
  exists, `capture.dir` is unrestricted.
- **Secrets inside it.** A secret is never written inline. A leaf written `{secret_file: /run/secrets/x}`
  is replaced, when the configuration is loaded, by a `Secret` holding that file's contents (with the
  trailing newline stripped, as for `_FILE`); the middleware calls `reveal()` where it uses the value. A
  file that can't be read is a `ConfigError` naming the key, never the contents. `mitm-proxy config`
  shows every `Secret` leaf as `********`, and 0002's effective-config report gives it as `{"set": true}`.

### 3. A built-in `capture` middleware

mitm-proxy ships `capture` (`mitm_proxy.middleware.capture:Capture`), a built-in name that is always
available, so it needs no `middleware.available` entry; it runs only when `middleware.chain` names it.
This reverses 0001's "Storing flows, replaying them, or a flow viewer UI" for **storing** only. Replay
and a viewer stay out of scope, and `Flow` still has no general serializer: the record format below
belongs to `capture`.

```yaml
middleware:
  chain: [capture]
  config:
    capture:
      dir: /captures            # one JSONL file per rule per UTC day: <rule>-YYYY-MM-DD.jsonl
      max_body_bytes: 1048576   # longer bodies are cut and marked "truncated": <original length>
      redact:
        headers: [authorization, proxy-authorization, x-api-key, cookie, set-cookie]  # on by default
        json_paths: ["$.api_key"]
        query_params: [api_key]
        marker: "[REDACTED]"
      rules:
        - name: inbound-api
          direction: inbound    # inbound | egress | any
          host: api.internal
          path: "/v1/**"
          methods: [POST]
```

The shape is as approved; this record only pins down defaults and matching:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `dir` | str | *(required)* | Where the files go. Created (mode `0700`) if missing. Under managed configuration (0002), must be under the bootstrap setting `capture.root`. |
| `max_body_bytes` | int | `1048576` | The most body bytes stored per message, after redaction. |
| `redact.headers` | list | the five above | Header names, any case. A given list **replaces** the default, so the example repeats it. |
| `redact.json_paths` | list | *(none)* | Paths whose values are replaced in JSON bodies and in JSON `data:` lines of server-sent events. |
| `redact.query_params` | list | *(none)* | Query parameter names whose values are replaced in the recorded URL. |
| `redact.marker` | str | `[REDACTED]` | What a redacted value becomes. |
| `rules` | list | *(required, one or more)* | Which flows are recorded. |
| `rules[].name` | str | *(required)* | Unique; lower-case letters, digits, `-` and `_` (it names files). |
| `rules[].direction` | str | `any` | `inbound`, `egress` or `any`. |
| `rules[].host` | str | any host | One allowlist pattern (0001's syntax: exact, or `*.suffix`). |
| `rules[].path` | str | any path | A glob on the path without the query: `*` within one segment, `**` across segments. |
| `rules[].methods` | list | any method | Methods, compared in upper case. |

An unknown key is a `StartupError` (a typo must not silently turn redaction off).

**Selection.** `applies_to` returns True when a rule matches the request head: `direction`, then
`host` (against `flow.host`; for a reverse flow, the upstream host), `path` and `methods`. The first
matching rule in order names the record; a flow is recorded once. Flows no rule matches keep the
unbuffered fast path.

**The record.** One JSON object per line, written from `on_done` (0006), so local answers, denies,
proxy errors and aborted flows are recorded too:

```json
{"rule": "inbound-api", "direction": "inbound", "flow": "9eba5076…", "outcome": "origin",
 "request": {"method": "POST", "url": "http://api.internal:8000/v1/answer",
             "headers": [["content-type", "application/json"], ["authorization", "[REDACTED]"]],
             "body": "{\"question\": \"…\"}"},
 "response": {"status": 200, "headers": [["content-type", "text/event-stream"]],
              "body": "data: {…}\n\ndata: [DONE]\n\n", "truncated": 2097152},
 "timings": {"start": "2026-11-03T10:15:02.123456Z", "ttfb_ms": 812.4,
             "end": "2026-11-03T10:15:09.551021Z"},
 "streamed": true}
```

(Shown wrapped; on disk it is one line.)

- `rule`, `direction`, `request` (method, URL, headers, body), `response` (status, headers, body; `null`
  when the flow ended without one), `timings` and `streamed` are the approved schema. `flow` (the flow
  ID, to join with the access log) and `outcome` (0006's `flow.outcome`, as the access log writes it)
  are added here.
- **Headers** are a list of `[name, value]` pairs, in order: a header can repeat.
- **Bodies** are as sent, after every middleware's chunk hooks. A body that decodes as UTF-8 is a
  string. Any other body is base64, with `"body_encoding": "base64"` beside it. A `gzip` or `deflate`
  `Content-Encoding` is decoded first (stdlib `zlib`), with `"content_decoded": "gzip"`; any other
  encoding is stored as it came, in base64. A body longer than `max_body_bytes` is cut there and gets
  `"truncated": <original length>`. A body whose hooks were skipped for `body.max_buffer_bytes` (0001)
  is `null`, with `"omitted": "oversize"`.
- **`streamed`** is true when the response went to the client chunk by chunk, unbuffered (decision
  4); its chunks are joined into one body.
- **Timings:** `start` and `end` are UTC wall-clock times (the flow's monotonic `started` and `complete`,
  0003, mapped onto the wall clock); `ttfb_ms` is `flow.timings.ttfb`, the origin's time to first byte,
  as in the access log. A stage that didn't happen is `null`.

**Redaction**, before anything is written or truncated:

- The header list applies to request and response headers, trailers too, even when `redact` is omitted.
- `json_paths` apply to bodies whose `Content-Type` is `application/json` or `*+json` and that parse as
  JSON (after decoding a `gzip`/`deflate` encoding). A path is `$` followed by `.name`, `[n]` and `[*]`
  steps, and `..name` for any depth. A body that isn't JSON, or doesn't parse, is stored as it is with
  `json_paths` skipped. Because skipping is silent, a configuration with `json_paths` and a rule that
  matches non-JSON bodies is worth testing: Writing-Middleware's capture section will say so.
- **Server-sent events.** In a `text/event-stream` body, `json_paths` apply to each `data:` line whose
  value parses as JSON; other lines are stored as they are. This comes with observe-only streaming (step
  (e)); until then an event-stream body is stored with `json_paths` skipped.
- **`query_params`** (default empty) apply to the request URL's query string: the value of every
  parameter with a listed name (compared exactly; query names are case-sensitive) becomes the marker,
  each time the name appears. The parameter names and the rest of the URL are kept.

**Files.** The record goes to `<dir>/<rule>-YYYY-MM-DD.jsonl`, the UTC date when it is written: rotation is
a new name at midnight UTC, and old files are never touched (retention is the operator's job). Files are
opened for append and created with mode `0600`. Each record is one `write` of one line, under a lock per
file, through `asyncio.to_thread` so the event loop never waits on the disk; `on_done`'s hook timeout
bounds it.

**Failure, fail open.** If `dir` can't be created or written, the startup logs a WARNING and `capture`
still loads. A record that can't be written (an `OSError`, a timeout) is dropped: `capture` catches it
itself, logs a WARNING at most once a minute per rule, and counts it in
`mitm_proxy_capture_records_total{rule, result="failed"}` (written records count as `result="written"`;
the `rule` values are declared from the configuration at build and reload, 0009). Traffic is never
affected, and `capture` keeps the default `on_error = "skip"`.

### 4. Observe-only streaming

The read-only part of 0001's deferred "true chunk-by-chunk streaming to middleware".

- **The flag.** A class attribute `observe_only: ClassVar[bool] = False` on `Middleware`, beside
  `on_error` and `timeout` (and reserved like them). True declares that the middleware never changes a
  body: its chunk hooks observe.
- **Mutation-capable** (0001) counts only middleware that override a chunk hook and are not observe-only,
  so an observe-only middleware doesn't force chunked framing.
- **When a flow streams.** If every middleware that applies to a flow is observe-only, neither body is
  buffered. Each chunk is written onward first and then passed, unchanged, to each applying middleware's
  chunk hook in chain order, awaited inline (0001's runner rule, so backpressure holds). The last call has
  `is_last=True`. `on_request` and `on_response` run on the heads, before the bodies; on those flows
  `read_all()` raises, and hooks that read the body must be chunk hooks.
- **Results.** An observe-only chunk hook returns `None`; bytes are an `InvalidResult` failure, and the
  middleware is skipped for the rest of the flow (fail open). Headers may still be changed in
  `on_request`/`on_response`, and `STOP`/`DENY` work as before, on the heads.
- **`observe_only` with `on_error = "fail"`** is refused when the middleware is registered (a
  `StartupError`): once the head has gone, a failure can't become a `502`.
- **Mixed flows.** If any applying middleware is not observe-only, the flow buffers as today, and
  observe-only middleware get the buffered body as one chunk. `body.max_buffer_bytes` applies only to
  buffered flows.
- `capture` is observe-only. Until this step lands it reads bodies with `read_all()` in `on_done` from
  the buffered flow, and `streamed` is always false.

### 5. `upstream.read_timeout_ms`

| Setting | Type | Default | Secret |
|---|---|---|---|
| `upstream.read_timeout_ms` | int | `120000` | no |

How long to wait for the next bytes from an origin: its response head, and each read of its body. It is
the pool's `read_timeout` (both protocols) and the WebSocket upgrade's response read; it is not a limit on
the whole response. The default is today's effective value (`proxy.idle_timeout_ms`'s default), so
nothing changes unless it is set. `proxy.idle_timeout_ms` keeps everything on the client side, the
upstream write timeout and the raw relay. A value of `300000` must work for slow LLM calls: an open
request already isn't ended by the client side's idle timer (h1 isn't reading; h2 skips the idle close
while a stream is open), and a test proves a 300 s silent origin under a shortened clock. An expired read
is the existing `504`. Reload applies it to reads that start afterwards, as for the pool's other timeouts.

### Implementation order

Each step is its own PR, with its tests and wiki changes, and each is useful alone:

1. **(a) Per-middleware configuration:** interpose's section setting first (its own record and
   release, then a pin bump here), then `middleware.config` and the `{secret_file: …}` leaves.
2. **(b) Reverse-proxy mode:** `reverse`, `ReverseProxySource`, `flow.direction`.
3. **(c) The `capture` middleware,** buffered: it reads bodies in `on_done`, and `streamed` is false.
   Header, `json_paths` and `query_params` redaction land here.
4. **(d) `upstream.read_timeout_ms`:** small and independent; can go earlier if slow calls hurt first.
5. **(e) Observe-only streaming:** the flag, the streaming path in `exchange.py`, and `capture` moved to
   chunk hooks with `observe_only = True`, with `json_paths` applied to server-sent events' `data:` lines.

## Alternatives considered

- **Forward mode only: configure the service's clients to use the proxy.** Doesn't fit a sealed network
  where the proxy is the only way in: the clients connect to an address, not through a proxy.
- **Each middleware reads its own YAML file, from a path in an environment variable.** A second
  configuration mechanism per middleware: no lookup order, no validation at load, no secret handling,
  no reload, and nothing in `mitm-proxy config` or 0002's effective config.
- **Ship `capture` as a separate package.** Redaction defaults and the record format are worth owning
  with the proxy, and a built-in name needs no import path in the configuration (0002 forbids module
  paths in managed configuration anyway).
- **Buffer the bodies for `capture`.** Holds a server-sent-event stream back until it ends, so the client
  sees nothing for the length of the call, and the recorded timings are the proxy's, not the origin's.
  Buffering stays only as step (c)'s first version.
- **Document `proxy.idle_timeout_ms` as the knob for slow origins.** Raising it to five minutes also
  keeps every idle client connection and tunnel open that long.
- **Pass the mapping as keyword arguments (`cls(**config)`).** Keys like `dir` would clash with
  builtins, and a typo becomes a `TypeError` far from its line. One mapping, validated by the middleware,
  reads better.
- **Redact after writing (a post-processing step).** Credentials would reach disk.
- **Detect body mutation at run time instead of declaring it.** 0001 rejected that for framing (it
  degrades quietly to buffering); a declared flag is checked once, at registration.

## Consequences

- **Easier:** recording an API service's inbound traffic and its LLM calls is configuration, not code;
  any middleware can take options; slow origins don't need a long client idle timeout.
- **Changes to earlier records:** 0001's out-of-scope list loses "storing flows" (replay and a viewer
  stay out), "per-middleware options in config" and the read-only half of "true chunk-by-chunk
  streaming". 0001's "an applying middleware forces buffering" gains the observe-only exception. 0002's
  bootstrap list gains `reverse` (and, when managed mode is built, `capture.root`); its managed list
  gains `middleware.config`.
- **Interface changes:** settings `reverse`, `middleware.config`, `upstream.read_timeout_ms` (and
  `capture.root` when managed mode is built); `Flow` gains `direction`; `Middleware` gains `observe_only`
  (reserved); constructors may take one mapping; a built-in `capture` name, with its options above
  (`redact.query_params` among them); the metric `mitm_proxy_capture_records_total`. A flow's direction
  (`inbound`/`egress`) is a different thing from the `direction` label (`request`/`response`) of
  `mitm_proxy_oversize_passthrough_total`; the wiki must keep them apart.
- **Security:** captures hold request and response bodies, which can be personal or secret data beyond
  what redaction catches. Files are `0600` in a `0700` directory, capture is opt-in per rule, and
  Security says so. A reverse listener exposes its upstream to whoever reaches its bind address.
- **interpose:** step (a) depends on an interpose release with section settings.
- **Rollback:** each step reverts alone; none changes behaviour unless configured (`reverse` empty,
  `capture` not in the chain, `upstream.read_timeout_ms` defaulting to today's value).
- **Wiki:** In-Depth-Configuration (`reverse`, `middleware.config`, secret leaves,
  `upstream.read_timeout_ms` in Timeouts), Writing-Middleware (constructor options, `observe_only`,
  streaming, `flow.direction`, a capture section), Architecture-Internals (the reverse source, the
  streaming path), Security (captures, reverse exposure), Deployment (reverse ports, the capture
  volume), Troubleshooting (capture write failures, 504 from slow origins), HTTP-API-Reference (the
  metric), Quick-Start (a reverse-proxy example). The example config gains the new keys.
- **TODO.md:** the rejected alternatives go into "Decided against".

## Open questions

All five decided 2026-11-03 by the brain coordinator for Stephen, each as recommended, and written into
the decision above.

1. ~~**Section settings in interpose, or mitm-proxy reading the subtree itself?**~~ Decided: interpose.
   Step (a) depends on an interpose release with section settings; the interpose record is written
   separately.
2. ~~**Is `middleware.config` managed (0002)?**~~ Decided: managed, like `middleware.chain`; `capture.dir`
   must be under a bootstrap setting `capture.root` when managed mode is built.
3. ~~**`json_paths` inside server-sent events.**~~ Decided: `json_paths` apply to each `data:` line that
   parses as JSON, in the observe-only streaming step (e).
4. ~~**Query-string redaction.**~~ Decided: `redact.query_params`, a list of parameter names whose values
   get the same marker; empty by default.
5. ~~**The forwarding headers.**~~ Decided: `X-Forwarded-Host` only if absent, `X-Forwarded-For`
   appended; no `Forwarded` (RFC 7239) and no `X-Forwarded-Proto`.
