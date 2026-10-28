# In-Depth Configuration

## Where configuration comes from

Every setting can be given in four places, and the first one that has it wins:

1. `MITM_PROXY_<KEY>_FILE`: the path of a file holding the value (Docker and Kubernetes secrets)
2. `MITM_PROXY_<KEY>`: an environment variable
3. the YAML config file: `MITM_PROXY_CONFIG` or `--config`, default `/etc/mitm-proxy/config.yaml`. A missing file is fine.
4. the built-in default

`<KEY>` is the setting's key in upper case, with the dots as underscores: `listen.port` is `MITM_PROXY_LISTEN_PORT`.
`mitm-proxy config` prints the effective value of every setting and where it came from, with secrets masked.

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
| `proxy.server_timing` | `MITM_PROXY_PROXY_SERVER_TIMING` | `False` | Add a `Server-Timing` header (connect, origin, middleware, proxy times) to every intercepted response, for browser devtools. Tells pages they were intercepted. |
| `intercept.hosts` | `MITM_PROXY_INTERCEPT_HOSTS` | *(none)* | The allowlist: hosts to intercept (`example.org`, `*.example.org`, IP literals). Everything else is tunnelled untouched. Empty: intercept nothing. |
| `middleware.available` | `MITM_PROXY_MIDDLEWARE_AVAILABLE` | *(none)* | Installed middleware that may run, as `name=module:attribute`. The built-in `log` is always available. |
| `middleware.chain` | `MITM_PROXY_MIDDLEWARE_CHAIN` | *(none)* | The middleware to run, by name, in order. |
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

### Middleware

`middleware.available` registers installed middleware by name, as `name=module:attribute` (a `Middleware` subclass, created with no arguments, or an instance). `middleware.chain` lists the names to run, in order. The built-in `log` is always available. A chain name that isn't registered is a configuration error. See [Writing Middleware](Writing-Middleware).

### Bodies and framing

When any middleware applies to a flow, its bodies are read whole before the middleware sees them (one chunk each), up to `body.max_buffer_bytes`. A larger body is **streamed through without middleware**, and logged as a warning and counted in the `mitm_proxy_oversize_passthrough_total` metric. Flows no middleware applies to are never buffered.

Memory: a body being buffered takes up to about twice its size for a moment, while it's assembled into one piece for middleware. A body that overflows the limit costs about the limit once. Budget roughly `2 × body.max_buffer_bytes` for each concurrent flow that middleware buffers.

If a middleware that can rewrite bodies (one with a chunk hook) applies to a flow, the flow's bodies are sent with `Transfer-Encoding: chunked` (HTTP/1.1) or without a `content-length` (HTTP/2), because the final length isn't known in advance. Responses to `HEAD`, and `1xx`, `204` and `304` responses, never get framing headers added.

### Timeouts

All times are in milliseconds.

- `proxy.peek_timeout_ms`: after a CONNECT to a listed host, how long to wait for the client's TLS ClientHello. Some protocols wait for the server to speak first, so a silent client isn't an error: the tunnel is relayed raw when the time is up.
- `proxy.idle_timeout_ms`: a client connection or tunnel with no traffic for this long is closed. It's also how long the proxy waits for an origin's response.
- `upstream.connect_timeout_ms`: connecting to an origin, TCP and TLS together, *including* waiting for a free connection when `upstream.max_connections_per_host` are all busy.
- `middleware.hook_timeout_ms`: a hook that takes longer has failed, and that middleware is skipped for the rest of the flow.

### Exposure

Both listeners default to `127.0.0.1`. To let other machines use the proxy, set `proxy.host` (e.g. `0.0.0.0`) **and** `proxy.auth`: without credentials, anyone who can reach the port can use your proxy to reach anything it can (the proxy warns at startup). Containers bind `0.0.0.0` inside, and the published ports decide what's reachable.

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

When `ca.key` isn't set, the key lives in `<state_dir>/ca/ca-key.pem` with mode `0600`; the proxy warns if it's readable by anyone else.

## Examples

A complete config file with every default is in [contrib/mitm-proxy.example.yaml](https://github.com/metalayerlab/mitm-proxy/blob/main/contrib/mitm-proxy.example.yaml).

**On your laptop, looking at one API.** Defaults bind to loopback; log every flow to the API and its subdomains:

```yaml
intercept:
  hosts: [api.example.org, "*.api.example.org"]
middleware:
  chain: [log]
```

**A shared proxy on a lab network,** with your own middleware and an internal CA the lab services use:

```yaml
proxy:
  host: 0.0.0.0          # with proxy.auth from MITM_PROXY_PROXY_AUTH_FILE
intercept:
  hosts: ["*.lab.example.org"]
middleware:
  available: [strip=labtools.middleware:StripTracking]
  chain: [strip, log]
upstream:
  extra_ca_file: /etc/mitm-proxy/lab-ca.pem
```

**Kubernetes,** with a stable CA from a Secret so clients don't have to re-trust it after every pod restart: the [Deployment](Deployment#kubernetes) manifests show `MITM_PROXY_CA_CERT_FILE` and `MITM_PROXY_CA_KEY_FILE` mounted from the `mitm-proxy` Secret.
