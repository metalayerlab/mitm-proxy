# HTTP API Reference

The server listens on port 8080 by default. The live OpenAPI schema is served at `/openapi.json`, and interactive docs at `/docs`.

## Endpoints

Generated from the code: don't edit the table, run `uv run scripts/gen-wiki.py`.

<!-- generated:start http-api -->
| Method | Path | Summary |
|---|---|---|
| `GET` | `/ca.der` | The root CA certificate (DER), to install on clients |
| `GET` | `/ca.pem` | The root CA certificate (PEM), to install on clients |
| `GET` | `/healthz` | Liveness: the process is up |
| `GET` | `/metrics` | Prometheus metrics |
| `GET` | `/readyz` | Readiness: the proxy listener is up and the CA is loaded |
| `GET` | `/v1/status` | Version, configuration, health and CA expiry |
<!-- generated:end http-api -->

`/healthz` (liveness) answers as long as the process runs. `/readyz` (readiness) answers 503 while the service can't serve, for example while a dependency is down. Orchestrators should restart on the first and route traffic on the second.

`/readyz` is 503 (`{"status": "starting"}`, `"stopping"` or `"stopped"`) until the proxy listener is up and the root CA is loaded, and again during shutdown.

### `GET /v1/status`

What's running, without secrets or hostnames (the configuration shows as a hash):

```json
{
  "version": "0.1.0",
  "mode": "standalone",
  "state": "ready",
  "started_at": "2026-10-28T14:28:33.156412+00:00",
  "config": {"hash": "sha256:a7230c…", "status": "APPLIED", "applied_at": "2026-10-28T14:28:33.156407+00:00"},
  "middleware": ["block-ads"],
  "health": {
    "proxy_listener": {"healthy": true, "status": "listening"},
    "ca": {"healthy": true, "status": "loaded"}
  },
  "certificates": {"ca": {"not_after": "2036-10-25T14:28:33+00:00", "fingerprint": "90:6A:8A:…"}}
}
```

The hash changes when any setting changes, except secrets, which count only as set or unset.

### `GET /metrics`

Prometheus text format (`text/plain; version=0.0.4`). No metric is labelled with a hostname.

| Metric | Type | Labels |
|---|---|---|
| `mitm_proxy_build_info` | gauge (1) | `version` |
| `mitm_proxy_client_connections` | gauge | |
| `mitm_proxy_flows_total` | counter | `type`: `http` (intercepted HTTPS), `plain_http`, `websocket` |
| `mitm_proxy_tunnels_total` | counter | `reason`: `out_of_scope`, `not_tls`, `sni_out_of_scope` |
| `mitm_proxy_middleware_failures_total` | counter | `middleware` |
| `mitm_proxy_oversize_passthrough_total` | counter | `direction`: `request`, `response` |
| `mitm_proxy_proxy_auth_failures_total` | counter | |
| `mitm_proxy_upstream_connections` | gauge | `protocol` |
| `mitm_proxy_config_applies_total` | counter | `result` |
| `mitm_proxy_certificate_expiry_timestamp_seconds` | gauge | `kind`: `ca` |

### `GET /ca.pem` and `GET /ca.der`

The root CA certificate that clients must trust: PEM (`application/x-pem-file`, as a download), or DER (`application/x-x509-ca-cert`, which phones offer to install). It's public: only the private key is secret.

## Authentication

None. The API is read-only: nothing on it changes state or reveals a secret, and it binds to `127.0.0.1` by default. The container manifests bind it to all interfaces inside the pod or container; the published port (Compose) and the NetworkPolicy (Kubernetes) decide who reaches it.

This is the admin API only. Proxy credentials (`proxy.auth`) protect the proxy port, not this one.

## Errors

Errors are JSON: `{"detail": "<message>"}`, with a 4xx status for a client mistake and 5xx for a server-side failure.

| Status | When | What to do |
|---|---|---|
| 404 | Unknown path (including the removed `/version` and `/stats`: use `/v1/status` and `/metrics`) | Check the path |
| 405 | A method other than `GET` | Use `GET` |
| 503 `{"status": "starting"}` | The proxy isn't serving yet, or is shutting down (`/readyz`); or the app runs without a proxy (only when rendering the schema) | Wait and retry; route no traffic |

The responses the *proxy* itself sends to clients (403, 407, 502, 504) are listed in [Troubleshooting](Troubleshooting#responses-from-the-proxy).
