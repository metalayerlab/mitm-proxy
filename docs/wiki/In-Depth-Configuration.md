# In-Depth Configuration

## Where configuration comes from

Every setting can be given in four places, and the first one that has it wins:

1. `MITM_PROXY_<KEY>_FILE`: the path of a file holding the value (Docker and Kubernetes secrets)
2. `MITM_PROXY_<KEY>`: an environment variable
3. the YAML config file: `MITM_PROXY_CONFIG` or `--config`, default `/etc/mitm-proxy/config.yaml`. A missing file is fine.
4. the built-in default

`<KEY>` is the setting's key in upper case, with the dots as underscores: `listen.port` is `MITM_PROXY_LISTEN_PORT`.
`mitm-proxy config` prints the effective value of every setting and where it came from, with secrets masked.

Each value must have its setting's type, wherever it comes from: there are no silent conversions. An integer setting takes a whole number (`3128`, not `3128.0` or `1_000` from a variable); a yes/no setting takes `true`/`false`, `yes`/`no`, `on`/`off` or `1`/`0`; a text setting takes a single value, not a YAML list or mapping; a list setting takes a YAML list or a comma-separated string. Anything else stops startup with `error: <key>: expected <type>`, naming where the value came from. A `_FILE` value loses its trailing whitespace (the final newline), nothing else. The engine is the [interpose](https://github.com/metalayerlab/interpose) library (design 0008).

## Settings

Generated from the code: don't edit the table, run `uv run scripts/gen-wiki.py`.

<!-- generated:start settings -->
| Key | Environment variable | Default | Description |
|---|---|---|---|
| `log_level` | `MITM_PROXY_LOG_LEVEL` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING` or `ERROR`. |
| `listen.host` | `MITM_PROXY_LISTEN_HOST` | `127.0.0.1` | Address the admin HTTP API binds to. |
| `listen.port` | `MITM_PROXY_LISTEN_PORT` | `8080` | Port the admin HTTP API listens on. |
| `state_dir` | `MITM_PROXY_STATE_DIR` | `/var/lib/mitm-proxy` | Directory for persistent state (the CA). |
| `proxy.host` | `MITM_PROXY_PROXY_HOST` | `127.0.0.1` | Address the proxy listens on. Anything but loopback without `proxy.auth` is an open proxy. |
| `proxy.port` | `MITM_PROXY_PROXY_PORT` | `3128` | Port the proxy listens on. |
| `proxy.auth` | `MITM_PROXY_PROXY_AUTH` | *(none)* | `user:password` for Basic proxy authentication. Empty: no authentication. 🔒 Secret: use the `_FILE` variant. |
| `proxy.peek_timeout_ms` | `MITM_PROXY_PROXY_PEEK_TIMEOUT_MS` | `1000` | How long to wait for a TLS ClientHello after CONNECT before relaying the tunnel raw. |
| `proxy.idle_timeout_ms` | `MITM_PROXY_PROXY_IDLE_TIMEOUT_MS` | `120000` | Close a client connection or tunnel after this long with no traffic. |
| `proxy.shutdown_grace_ms` | `MITM_PROXY_PROXY_SHUTDOWN_GRACE_MS` | `10000` | On SIGTERM, how long in-flight connections get to finish. |
| `proxy.access_log` | `MITM_PROXY_PROXY_ACCESS_LOG` | `False` | Log one line per intercepted flow (`mitm_proxy.flows`): sizes, timings and how it ended. |
| `proxy.access_log_relayed` | `MITM_PROXY_PROXY_ACCESS_LOG_RELAYED` | `False` | Log one line per flow relayed without interception (`mitm_proxy.relayed`): tunnels to unlisted hosts and plain HTTP to them. Records every destination. |
| `proxy.server_timing` | `MITM_PROXY_PROXY_SERVER_TIMING` | `False` | Add a `Server-Timing` header (connect, origin, middleware, proxy times) to every intercepted response, for browser devtools. Tells pages they were intercepted. |
| `intercept.hosts` | `MITM_PROXY_INTERCEPT_HOSTS` | *(empty list)* | The allowlist: hosts to intercept (`example.org`, `*.example.org`, IP literals). Everything else is tunnelled untouched. Empty: intercept nothing. A list: YAML, or comma-separated. |
| `reverse` | `MITM_PROXY_REVERSE` | *(empty list)* | Reverse-proxy listeners. Each item has `listen` (the `host:port` this proxy binds) and `upstream` (an `http://` or `https://` host, optional port) it forwards to; every request on a listener is intercepted and sent to its upstream. From the config file or `_FILE`, or the indexed variables `MITM_PROXY_REVERSE_<N>_LISTEN` and `_<N>_UPSTREAM`. Empty: no reverse listeners. A list: YAML, or comma-separated. |
| `middleware.available` | `MITM_PROXY_MIDDLEWARE_AVAILABLE` | *(empty list)* | Installed middleware that may run, as `name=module:attribute`. A list: YAML, or comma-separated. |
| `middleware.chain` | `MITM_PROXY_MIDDLEWARE_CHAIN` | *(empty list)* | The middleware to run, by name, in order. A list: YAML, or comma-separated. |
| `middleware.config` | `MITM_PROXY_MIDDLEWARE_CONFIG` | *(none)* | Options for each middleware, by name: `<name>: {...}`, passed to its class's constructor. A `{secret_file: <path>}` leaf is read as a secret. From the config file or `_FILE`, never a plain variable. |
| `middleware.hook_timeout_ms` | `MITM_PROXY_MIDDLEWARE_HOOK_TIMEOUT_MS` | `5000` | A middleware hook that runs longer than this has failed, and is skipped for the flow. |
| `body.max_buffer_bytes` | `MITM_PROXY_BODY_MAX_BUFFER_BYTES` | `67108864` | The largest body buffered for middleware. A larger body skips middleware and is streamed through. |
| `ca.cert` | `MITM_PROXY_CA_CERT` | *(none)* | Root CA certificate (PEM), usually via `_FILE`. Empty: `<state_dir>/ca/ca.pem`, generated on first start. |
| `ca.key` | `MITM_PROXY_CA_KEY` | *(none)* | Root CA private key (PEM). Set together with `ca.cert`, or not at all. 🔒 Secret: use the `_FILE` variant. |
| `ca.common_name` | `MITM_PROXY_CA_COMMON_NAME` | `mitm-proxy CA` | Common name of a newly generated root CA, as clients' trust stores show it. |
| `ca.leaf_cache_size` | `MITM_PROXY_CA_LEAF_CACHE_SIZE` | `1024` | Leaf certificates kept in memory. |
| `upstream.extra_ca_file` | `MITM_PROXY_UPSTREAM_EXTRA_CA_FILE` | *(none)* | PEM bundle of extra CAs trusted for origin servers, on top of the system store. |
| `upstream.connect_timeout_ms` | `MITM_PROXY_UPSTREAM_CONNECT_TIMEOUT_MS` | `10000` | Connecting to an origin (TCP and TLS), including any wait for a free pool slot. |
| `upstream.idle_timeout_ms` | `MITM_PROXY_UPSTREAM_IDLE_TIMEOUT_MS` | `60000` | Close a pooled upstream connection after this long unused. |
| `upstream.max_connections_per_host` | `MITM_PROXY_UPSTREAM_MAX_CONNECTIONS_PER_HOST` | `6` | The most upstream connections kept to one host, port and SNI. |
<!-- generated:end settings -->

### The allowlist

Only hosts listed in `intercept.hosts` are intercepted; every other tunnel is relayed untouched. Entries are:

| Entry | Matches |
|---|---|
| `example.org` | exactly `example.org` |
| `*.example.org` | any subdomain, at any depth (`a.example.org`, `a.b.example.org`), but **not** `example.org` itself: list both if you want both |
| `192.0.2.10`, `2001:db8::1` | that IP address (CONNECT to an IP literal) |

Matching ignores case and a trailing dot, and international names are compared in their `xn--` form. Ports don't matter: a listed host is intercepted on every port. A lone `*` is refused: intercepting everything is not supported. `mitm-proxy scope check HOST` tells you what would happen to a host.

The TLS name the client asks for (SNI) must be on the list too, or the tunnel is relayed raw: no certificate is ever made for a name nobody listed.

### Reverse-proxy mode

`intercept.hosts` is for the **forward** proxy, where clients are configured to use mitm-proxy. A **reverse** listener is the other way round: mitm-proxy binds an address of its own and sends every request that arrives there to one fixed upstream, so a service's own clients reach it without any proxy configuration. This is how you record a service's inbound traffic.

```yaml
reverse:
  - listen: 0.0.0.0:8081
    upstream: http://api.internal:8000
  - listen: "[::1]:8082"
    upstream: https://llm-gateway.internal
```

- `listen` is the `host:port` this listener binds (`[::1]:8081` for an IPv6 literal); each must be unique. `upstream` is `http://` or `https://`, a host and an optional port, with no path, query or user info.
- **Every request on a reverse listener is intercepted** and sent to its upstream: `intercept.hosts` does not apply, and there is no proxy authentication (clients of a reverse proxy don't send `Proxy-Authorization`). The listener reaches only its one upstream; its exposure is the bind address, like any service — see [Security](Security). Middleware still selects with `applies_to`, and these flows have `flow.direction == "inbound"` (the forward proxy's are `"egress"`; see [Writing Middleware](Writing-Middleware)).
- The origin is told the upstream's authority as its `Host`. The client's own `Host` is kept in `X-Forwarded-Host` (only when that header is absent), and the client's address is appended to `X-Forwarded-For`. Nothing else is added: no `Forwarded` and no `X-Forwarded-Proto` (a TLS terminator in front sets that itself).
- Listeners are bound at startup; one that can't bind stops startup, and `/readyz` is ready only once every listener is bound. A change to `reverse` applies on **restart** — a reload logs a warning and keeps the running listeners.
- Plain HTTP/1.1 only, for now. TLS and HTTP/2 on a reverse listener are later; until then put a TLS terminator (Caddy, for example) in front and forward plain HTTP.

`reverse` comes from the config file or from `MITM_PROXY_REVERSE_FILE` (a YAML file holding the whole list), or from the indexed variables `MITM_PROXY_REVERSE_<N>_LISTEN` and `MITM_PROXY_REVERSE_<N>_UPSTREAM` (for `N` from 0).

### Middleware

`middleware.available` registers installed middleware by name, as `name=module:attribute` (a `Middleware` subclass, or an instance). `middleware.chain` lists the names to run, in order. A chain name that isn't registered (or built in) is a configuration error. One middleware is built in: `capture` ([Capture](#capture)), always available, so it needs no `middleware.available` entry, and it runs only when the chain names it. A `middleware.available` entry named `capture` is a configuration error, because it would shadow the built-in. The old built-in `log` became the `proxy.access_log` setting, and a chain that still names `log` fails at startup with a message saying so. See [Writing Middleware](Writing-Middleware).

#### Options for a middleware

`middleware.config` gives a middleware its own options, by name. Each entry is a mapping, free-form: whatever that middleware accepts.

```yaml
middleware:
  available: [strip=labtools.middleware:StripTracking]
  chain: [strip]
  config:
    strip:
      params: [utm_source, utm_medium]
      report: { url: https://lab.example.org/hits, token: { secret_file: /run/secrets/strip-token } }
```

- The class is constructed with its entry as one argument, a read-only mapping (nested mappings and lists are read-only too). A middleware with no entry is constructed with no arguments, as before.
- An entry for a name that isn't in `middleware.available` or built in is a configuration error. An entry that isn't a mapping is too.
- The middleware checks its own options. If it refuses them (or takes no options), or if the name is registered as an instance (there's nothing to construct), startup fails with a line naming the middleware, and a new configuration is refused, keeping the running one.
- The middleware is constructed again whenever a configuration is applied, so changed options apply to flows that start afterwards.
- `middleware.config` comes from the config file, or from `MITM_PROXY_MIDDLEWARE_CONFIG_FILE`: the path of a YAML file holding the whole mapping (a Kubernetes ConfigMap or Secret mount). A plain `MITM_PROXY_MIDDLEWARE_CONFIG` variable is an error: a mapping in one variable is unreadable.
- Never write a secret inline. Write the leaf as `{ secret_file: <path> }`: the file is read when the configuration is loaded (the trailing newline stripped) and the middleware gets a secret value, shown as `********` (see [Secrets](#secrets)). A file that can't be read is a configuration error naming the key, never the contents.

#### Capture

The built-in `capture` middleware records the flows its rules match as JSON Lines files, with credentials redacted before anything is written (design 0012). Put `capture` in the chain and give it options:

```yaml
middleware:
  chain: [capture]
  config:
    capture:
      dir: /var/lib/mitm-proxy/captures
      max_body_bytes: 1048576
      redact:
        headers: [authorization, proxy-authorization, x-api-key, cookie, set-cookie]
        json_paths: ["$.api_key", "$..token"]
        query_params: [api_key]
        marker: "[REDACTED]"
      rules:
        - name: inbound-api
          direction: inbound
          host: api.internal
          path: "/v1/**"
          methods: [POST]
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `dir` | text | *(required)* | Where the files go: `<dir>/<rule>-YYYY-MM-DD.jsonl`, one per rule per UTC day. Created with mode `0700` if missing. |
| `max_body_bytes` | whole number | `1048576` | The most body bytes stored per message, after redaction. A longer body is cut and marked `"truncated": <its length>`. |
| `redact.headers` | list | `authorization`, `proxy-authorization`, `x-api-key`, `cookie`, `set-cookie` | Header (and trailer) names whose values are replaced, any case. A list you give **replaces** the default, so repeat the ones you still want. |
| `redact.json_paths` | list | none | Values replaced in JSON bodies: `$` followed by `.name`, `[n]`, `[*]` and `..name` (that name at any depth). |
| `redact.query_params` | list | none | Query parameter names whose values are replaced in the recorded URL, every time the name appears. Names are case-sensitive and kept. |
| `redact.marker` | text | `[REDACTED]` | What a redacted value becomes. |
| `rules` | list | *(required, one or more)* | Which flows are recorded. The first rule that matches names the record; a flow is recorded once. |
| `rules[].name` | text | *(required)* | Unique; lower-case letters, digits, `-` and `_` (it names the files). |
| `rules[].direction` | text | `any` | `inbound` (a [reverse listener](#reverse-proxy-mode)'s flows), `egress` (the forward proxy's) or `any`. |
| `rules[].host` | text | any host | One pattern in the [allowlist](#the-allowlist)'s syntax: exact, or `*.suffix`. For a reverse flow it's the upstream's host. |
| `rules[].path` | text | any path | A glob on the path without its query: `*` within one segment, `**` across segments. |
| `rules[].methods` | list | any method | Methods, in any case. |

- **Any unknown key**, at any level, stops startup (or refuses a new configuration): a typo such as `redcat:` must never quietly turn redaction off.
- **Selection:** a flow no rule matches isn't buffered for capture at all. One that matches is buffered like any flow middleware applies to ([Bodies and framing](#bodies-and-framing)), and this version of capture always records `"streamed": false`.
- **Redaction** happens before anything is written or cut: the header list applies to request and response headers and trailers, even with no `redact` key. `json_paths` apply only to bodies whose `Content-Type` is `application/json` or `*+json` and that parse as JSON (after a `gzip` or `deflate` encoding is decoded). Any other body, a server-sent-event stream (`text/event-stream`) included, is stored with `json_paths` **skipped, silently**: test the configuration against the traffic it will record (see [Writing Middleware](Writing-Middleware#the-built-in-capture-middleware)).
- **If `dir` can't be created or written,** startup logs a warning and the proxy runs; each record that can't be written is dropped, counted in `mitm_proxy_capture_records_total{result="failed"}` and warned about at most once a minute per rule. Capture never affects the traffic.
- Old files are never touched: deleting them (retention) is yours to do.

### Bodies and framing

When any middleware applies to a flow, its bodies are read whole before the middleware sees them (one chunk each), up to `body.max_buffer_bytes`. A larger body is **streamed through without middleware**, and logged as a warning and counted in the `mitm_proxy_oversize_passthrough_total` metric. Flows no middleware applies to are never buffered.

Memory: a body being buffered takes up to about twice its size for a moment, while it's assembled into one piece for middleware. A body that overflows the limit costs about the limit once. Budget roughly `2 × body.max_buffer_bytes` for each concurrent flow that middleware buffers.

If a middleware that can rewrite bodies (one with a chunk hook) applies to a flow, the flow's bodies are sent with `Transfer-Encoding: chunked` (HTTP/1.1) or without a `content-length` (HTTP/2), because the final length isn't known in advance. Responses to `HEAD`, and `1xx`, `204` and `304` responses, never get framing headers added.

### Timeouts

All times are in milliseconds.

- `proxy.peek_timeout_ms`: after a CONNECT to a listed host, how long to wait for the client's TLS ClientHello. Some protocols wait for the server to speak first, so a silent client isn't an error: the tunnel is relayed raw when the time is up.
- `proxy.idle_timeout_ms`: a client connection or tunnel with no traffic for this long, and no request in progress, is closed. A request in progress is bounded on its own: a request body that stops arriving, a client that stops reading the response (on HTTP/2, gives no flow-control window), or an origin that doesn't answer each end that request after this long. So does a write to either side that makes no progress (a peer that stopped reading its socket). On HTTP/2 the connection's other streams carry on. A slow origin gets its `504`, not a dropped connection.
- `upstream.connect_timeout_ms`: connecting to an origin, TCP and TLS together, *including* waiting for a free connection when `upstream.max_connections_per_host` are all busy.
- `middleware.hook_timeout_ms`: a hook that takes longer has failed, and that middleware is skipped for the rest of the flow.

### Exposure

Both listeners default to `127.0.0.1`. To let other machines use the proxy, set `proxy.host` (e.g. `0.0.0.0`) **and** `proxy.auth`: without credentials, anyone who can reach the port can use your proxy to reach anything it can (the proxy warns at startup). Containers bind `0.0.0.0` inside, and the published ports decide what's reachable.

A reverse listener (`reverse`) binds the address you give it, and has no authentication: whoever can reach it reaches its one upstream. Its only guard is the bind address, so bind it where only the clients you mean can reach it.

### The access log

`proxy.access_log: true` writes one INFO line per intercepted flow on the `mitm_proxy.flows` logger, once the flow has ended:

```text
9eba5076… h2>h2 GET https://example.org/a.png 200 req=0 resp=21292 total=79.6ms connect=0.0ms(reused) ttfb=79.3ms mw=0.1ms outcome=origin
```

After the flow ID: the client's protocol `>` the origin's, the method, URL and status, then:

| Field | Meaning |
|---|---|
| `req`, `resp` | Body bytes read from the client and written to the client (after any rewrite) |
| `total` | From the request headers arriving to the last byte sent |
| `connect`, `ttfb`, `mw` | Waiting for an upstream connection (`(reused)` if pooled), the origin's share, time in middleware hooks. See [Troubleshooting](Troubleshooting#requests-are-slow-the-proxy-the-origin-or-a-middleware) |
| `outcome` | How the flow ended (below) |

| `outcome` | Meaning |
|---|---|
| `origin` | The origin's response was sent |
| `local:<name>` | Middleware `<name>` answered the request itself |
| `denied:<name>` | Middleware `<name>` denied it (`403` on the request, `502` on the response) |
| `error:<status>` | The proxy answered with its own error (`502`, `504`) |
| `aborted` | The flow ended before the response was complete: the client reset or disconnected, or an I/O error. The status is the one already sent, or `-` |
| `websocket` | A WebSocket, logged when it closes; `req`/`resp` are the bytes each way |

A stage that didn't happen prints `-`. Logging adds no buffering: flows no middleware applies to are still streamed.

`proxy.access_log_relayed: true` logs what the proxy relays **without** intercepting, on the `mitm_proxy.relayed` logger. Turn it on or off independently of `proxy.access_log`:

```text
tunnel 4f0c… 192.0.2.10:51234 example.net:443 out_of_scope up=1830 down=48211 total=5012.3ms
http 7a1d… 192.0.2.10:51240 GET http://example.net/ 200 req=0 resp=1256 total=88.1ms
```

- `tunnel` lines are written when a tunnel closes. The reason is `out_of_scope` (host not listed), `not_tls`, `sni_out_of_scope`, or `error:<status>` when the proxy couldn't connect. `up` and `down` are the bytes from and to the client. Nothing inside a tunnel is visible.
- `http` lines are plain-HTTP requests to unlisted hosts.

It records every destination and client address, so it's off by default. See [Security](Security).

### Server-Timing

`proxy.server_timing: true` adds one `Server-Timing` field to every intercepted response, so a browser's devtools (Network → a request → Timing) show where the time went:

```text
server-timing: mitm-connect;dur=0.0;desc="reused", mitm-origin;dur=79.3, mitm-mw;dur=0.4, mitm-proxy;dur=80.1
```

| Metric | Meaning | Present when |
|---|---|---|
| `mitm-connect` | Waiting for an upstream connection; `desc` is `reused` or `new` | A connection was leased |
| `mitm-origin` | The origin's share: request sent to response headers | The origin answered |
| `mitm-mw` | Time in middleware hooks so far | Middleware ran |
| `mitm-local` | `desc` names the middleware that answered or denied the request | Middleware answered |
| `mitm-proxy` | From the request arriving to the response head going out | Always |

`mitm-proxy` is not a total: a header goes out before the body, so the body's transfer isn't in it (the browser shows that as Content Download). The field covers every intercepted flow, whether or not middleware applies (it adds no buffering), including responses middleware answers itself, denies, and the proxy's own `502`/`504`. It's never added to relayed tunnels or WebSocket upgrades. An origin's own `Server-Timing` is kept; the proxy's field follows it, and the `mitm-` prefix keeps the names apart.

It's off by default because it tells every intercepted page that it was intercepted. See [Security](Security).

### The CA and origin trust

The root CA comes from `ca.cert` + `ca.key` if set (both, or neither), otherwise from `<state_dir>/ca/`, where it's generated on first start. `upstream.extra_ca_file` adds CAs for origins with private certificates (an internal lab CA) **on top of** the system trust store. There is no way to switch off verification of origin certificates.

## Secrets

Never put a secret in the config file or in a plain environment variable that `docker inspect` or `ps` would show. Use the `_FILE` variant and mount the value as a file:

- Docker: `MITM_PROXY_<KEY>_FILE=/run/secrets/<name>` with a secrets file or bind mount
- Kubernetes: a Secret mounted as a volume (SOPS-encrypted in the GitOps repo)
- systemd: `LoadCredential=` and `%d/<name>`

This project's secrets:

| Setting | What it grants | Supply it as |
|---|---|---|
| `proxy.auth` | Use of the proxy (`user:password`) | `MITM_PROXY_PROXY_AUTH_FILE` |
| `ca.key` | Signing certificates that every client trusting the root accepts: the most sensitive value here | `MITM_PROXY_CA_KEY_FILE`, with `MITM_PROXY_CA_CERT_FILE` for the (public) certificate |

Inside the proxy a secret is a `Secret` value, which prints as `********` in logs, errors and `repr()`; only the code that authenticates clients or loads the CA reads the value itself. The status endpoint and the configuration hash record only whether a secret is set.

A middleware's secret options are `{ secret_file: <path> }` leaves in `middleware.config` ([Options for a middleware](#options-for-a-middleware)). Each becomes a `Secret` too: `mitm-proxy config` shows it as `********`, and the configuration hash records only whether it's set. These leaves are read only from local configuration (the config file or `MITM_PROXY_MIDDLEWARE_CONFIG_FILE`).

When `ca.key` isn't set, the key lives in `<state_dir>/ca/ca-key.pem` with mode `0600`; the proxy warns if it's readable by anyone else.

## Examples

A complete config file with every default is in [contrib/mitm-proxy.example.yaml](https://github.com/metalayerlab/mitm-proxy/blob/main/contrib/mitm-proxy.example.yaml).

**On your laptop, looking at one API.** Defaults bind to loopback; log every flow to the API and its subdomains:

```yaml
proxy:
  access_log: true
intercept:
  hosts: [api.example.org, "*.api.example.org"]
```

**A shared proxy on a lab network,** with your own middleware and an internal CA the lab services use:

```yaml
proxy:
  host: 0.0.0.0          # with proxy.auth from MITM_PROXY_PROXY_AUTH_FILE
intercept:
  hosts: ["*.lab.example.org"]
middleware:
  available: [strip=labtools.middleware:StripTracking]
  chain: [strip]
upstream:
  extra_ca_file: /etc/mitm-proxy/lab-ca.pem
```

**Kubernetes,** with a stable CA from a Secret so clients don't have to re-trust it after every pod restart: the [Deployment](Deployment#kubernetes) manifests show `MITM_PROXY_CA_CERT_FILE` and `MITM_PROXY_CA_KEY_FILE` mounted from the `mitm-proxy` Secret.
