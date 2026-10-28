# Troubleshooting

## Diagnostics

- **Effective configuration:** `mitm-proxy config` prints every setting and where it came from (default, file, env var). Secrets are masked.
- **More logging:** `MITM_PROXY_LOG_LEVEL=DEBUG`.
- **Health:** `curl http://127.0.0.1:8080/healthz` (alive) and `/readyz` (ready to serve).
- **Container:** `docker logs mitm-proxy`, and `docker inspect --format '{{json .State.Health}}' mitm-proxy`.
- **Kubernetes:** `kubectl -n mitm-proxy logs deploy/mitm-proxy` and `kubectl -n mitm-proxy describe pod`.
- **systemd:** `journalctl -u mitm-proxy -e` and `systemctl status mitm-proxy`.

## Common problems

### The client says the certificate isn't trusted

The client doesn't trust the proxy's root CA. Install it: download `http://127.0.0.1:8080/ca.pem` (or `/ca.der`), or run `mitm-proxy ca show`. The proxy logs `TLS handshake with … failed …: if the client rejected the certificate, install the root CA on it`. Some apps pin certificates and will never accept it: remove their hosts from `intercept.hosts`.

### A host isn't intercepted (no flow, no middleware)

Run `mitm-proxy scope check HOST`. Remember `*.example.org` doesn't match `example.org` itself. The client's TLS name (SNI) must be listed too: `mitm_proxy_tunnels_total{reason="sni_out_of_scope"}` counts these. Apps that ignore the system proxy settings, or use HTTP/3, never reach the proxy at all.

### `502 … certificate verification failed`

The origin's certificate doesn't verify: it's self-signed or from a private CA, or doesn't name the host. Add the CA to `upstream.extra_ca_file`. Verification can't be switched off.

### Every request gets `407 Proxy Authentication Required`

`proxy.auth` is set and the client sends no or wrong credentials. Use `http://user:password@host:3128` as the proxy URL, or `curl --proxy-user`.

### An origin answers `411 Length Required`

A middleware that rewrites bodies applies to the flow, so the request body is sent chunked, and this origin refuses chunked uploads. Narrow the middleware with `applies_to` so it skips these requests.

### `middleware 'x' failed in …` / `timed out` in the log

The middleware raised or ran past `middleware.hook_timeout_ms`. The flow carried on without it. Fix the middleware; the traceback is in the log. A middleware that *blocks* the event loop can't be timed out and stalls everything: make it async.

### Requests are slow: the proxy, the origin or a middleware?

Turn on the access log (`proxy.access_log: true`) and read the end of each flow line:

```text
… 200 req=0 resp=1256 total=812.4ms connect=153.0ms ttfb=640.2ms mw=0.4ms outcome=origin
```

- **`ttfb`** is the origin: from the request being sent to its response headers. If it's most of `total`, the origin is slow, not the proxy.
- **`connect`** is waiting for an upstream connection. Without `(reused)`, it's a new TCP and TLS connection, and high values point at DNS or the network to the origin. With `(reused)` it's normally about 0; a high value means the request queued for a free connection because the host had `upstream.max_connections_per_host` busy ones.
- **`mw`** is time spent in middleware hooks. To find which one, read `flow.timings.middleware` (see [Writing Middleware](Writing-Middleware)).
- **The rest** of `total` is the proxy's own work plus moving the bodies: receiving the response from the origin and sending it to the client. A large remainder on a big response is the download, not the proxy.

`-` means that stage didn't happen (for example, `connect=-` on a response a middleware sent itself, `outcome=local:<name>`).

In a browser, set `proxy.server_timing: true` and open devtools (Network → a request → Timing): the same breakdown appears under Server Timing as `mitm-connect`, `mitm-origin`, `mitm-mw` and `mitm-proxy`, and `mitm-local` names the middleware that answered a request itself. These show up even for responses no middleware ran on. See [In-Depth Configuration](In-Depth-Configuration).

### A flow ends with `outcome=aborted`

The flow ended before the whole response was sent. Usually the client gave up: a browser cancels image and media requests it no longer needs (an HTTP/2 stream reset), or the user navigated away. Occasional `aborted` lines are normal. If every flow to one host ends that way, check the proxy's warnings for that flow ID: an origin that stops sending part-way also ends here.

### `… body is over body.max_buffer_bytes …; streaming it through without middleware`

That body was bigger than `body.max_buffer_bytes`, so middleware didn't see it. Raise the limit if you need to inspect such bodies (they're held in memory).

### `the proxy listens on 0.0.0.0 without proxy.auth`

Anyone who can reach the port can use the proxy. Set `proxy.auth`, or bind `127.0.0.1`.

### `error: cannot listen on 127.0.0.1:3128: Address already in use`

Another process has the port (`ss -ltnp 'sport = :3128'`). Change `proxy.port` or `listen.port`.

### Clients must re-trust the CA after every restart

The CA is generated on first start in `<state_dir>/ca/`, and `state_dir` isn't persistent (a container without a volume, a pod on `emptyDir`). Persist it, or supply the CA with `ca.cert`/`ca.key`.

### Responses from the proxy

Responses the proxy sends itself, with a one-line plain-text body:

| Status | Meaning |
|---|---|
| 400 | Malformed request, a relative URL sent to the proxy, or CONNECT without `host:port` |
| 403 | `denied by middleware <name>` (on the request) |
| 405 | CONNECT where it isn't allowed |
| 407 | Proxy credentials missing or wrong |
| 501 | CONNECT over HTTP/2 (not supported) |
| 502 | The origin couldn't be reached, failed verification, or broke the connection; `denied by middleware <name>` (on the response); a middleware stopped the request without a response |
| 504 | The origin didn't connect or answer in time |

### `error: cannot read MITM_PROXY_..._FILE`

The file named by a `_FILE` variable doesn't exist, or the process can't read it. Check the path inside the container or unit, and the file's owner and mode: the process runs as uid 1000 / the `mitm-proxy` user.
