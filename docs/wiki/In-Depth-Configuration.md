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

<!-- guidance: below the table, explain anything a one-line description can't: how settings interact, valid ranges, performance trade-offs. -->

## Secrets

Never put a secret in the config file or in a plain environment variable that `docker inspect` or `ps` would show. Use the `_FILE` variant and mount the value as a file:

- Docker: `MITM_PROXY_<KEY>_FILE=/run/secrets/<name>` with a secrets file or bind mount
- Kubernetes: a Secret mounted as a volume (SOPS-encrypted in the GitOps repo)
- systemd: `LoadCredential=` and `%d/<name>`

<!-- guidance: list this project's secrets, or say there are none. -->

## Examples

A complete config file with every default is in [contrib/mitm-proxy.example.yaml](https://github.com/metalayerlab/mitm-proxy/blob/main/contrib/mitm-proxy.example.yaml).

<!-- guidance: two or three real-world configurations, each with a sentence on when you'd use it. -->
