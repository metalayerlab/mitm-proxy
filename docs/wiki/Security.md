# Security

## Threat model

mitm-proxy decrypts traffic on purpose, so the question is who else can use that power.

**What it protects:**

- **The root CA key.** Whoever holds it can impersonate *any* site to every client that trusts the root. It's the most valuable thing here.
- **The proxy itself.** Anyone who can use it can reach whatever the proxy can reach (your lab network included) and, for listed hosts, read and change the traffic.
- **Intercepted traffic.** It's plaintext inside the process, in middleware, and in whatever middleware logs.

**Exposed:**

- the proxy port (`proxy.port`, default 3128, on `127.0.0.1`);
- the admin API (`listen.port`, default 8080, on `127.0.0.1`, read-only).

**Trust:**

- Clients are trusted only as far as `proxy.auth` says.
- Origins are always verified against the system trust store plus `upstream.extra_ca_file`.
- Middleware is trusted completely: it runs in the proxy process, with its access.

**What an attacker gains:**

- With the CA key: silent interception of anyone who trusts the root.
- With access to the proxy port: an open relay into your network, and interception of listed hosts' traffic as it passes.
- With the admin API: the version, configuration hash and counters (no secrets, no hostnames).

**What the proxy reveals to clients:** by default, only what interception needs (its certificates, and its own error responses: `403`, `407`, `502`, `504`). The access log (`proxy.access_log`) records each intercepted request's URL, including its query string, which can hold tokens; treat the log as sensitive. `proxy.access_log_relayed` also records every destination host and client address the proxy relays for, so it's off by default. With `proxy.server_timing` on, every intercepted response also carries the proxy's timings, and middleware names when middleware answered. A page's own scripts can read them (`PerformanceResourceTiming.serverTiming`) and report them to the site, so a site can tell it was intercepted. The proxy never sends `Timing-Allow-Origin`, so other sites' scripts can't read them. Turn it on deliberately, for debugging.

**Out of scope:**

- clients that choose to trust the root;
- what your middleware does with the plaintext;
- denial of service (there's no rate limiting). Stalled clients are bounded, though: a request whose client stops sending or reading ends after `proxy.idle_timeout_ms`, and an idle connection with no request in progress is then closed, so a stalled client can't hold resources forever.

## Secrets

- Secrets are read from files only (`MITM_PROXY_<KEY>_FILE`), never from the config file. See [In-Depth Configuration](In-Depth-Configuration#secrets).
- Values are never logged. Logs and `mitm-proxy config` show where a value came from, never the value itself.
- CI scans every commit with gitleaks, and secrets in history fail the build.

| Secret | Grants | Rotate |
|---|---|---|
| `ca.key` (or `<state_dir>/ca/ca-key.pem`) | Impersonating any site to clients that trust the root | `mitm-proxy ca init --force` (or a new Secret), restart, then remove the old root from every client and install the new one. Remove the old root even if you don't rotate, once you stop using the proxy. |
| `proxy.auth` | Using the proxy | Change the value in its secret file and restart the proxy; update clients. |

The leaf certificates' shared key is generated at every start and never leaves the process unencrypted: on disk it's encrypted with a random password held only in memory.

## Hardening

- **Container:** runs as uid 1000, with a read-only root filesystem and all capabilities dropped (Compose/Kubernetes manifests).
- **Kubernetes:** *restricted* Pod Security Standard, `seccompProfile: RuntimeDefault`, no service-account token, and a NetworkPolicy that admits only the ingress controller.
- **systemd:** a dedicated system user, `ProtectSystem=strict`, no capabilities, a system-call filter, and restricted address families.
- **Supply chain:** dependencies are pinned in the lock file and updated weekly by Dependabot. GitHub Actions are pinned to commit SHAs.

- **Fail closed:** only allowlisted hosts are intercepted; a lone `*` is refused; no certificate is minted for an unlisted name (the SNI is checked too). Leaf certificates name exactly one host and last 30 days.
- **Upstream verification:** always on, with hostname checking; there is no setting, flag or code path to disable it.
- **Defaults:** both listeners bind `127.0.0.1`. Binding the proxy elsewhere without `proxy.auth` logs a warning at startup. The Kubernetes NetworkPolicy leaves the proxy port closed until you add a rule.
- **Proxy authentication:** Basic credentials, compared in constant time; `Proxy-Authorization` is never forwarded to origins. Basic is clear text between client and proxy: use it on trusted networks only.
- **Input:** HTTP/1.1 is parsed by h11 and HTTP/2 by h2, both strict; request heads are limited to 64 KiB, peeked ClientHellos to 64 KiB, bodies buffered for middleware to `body.max_buffer_bytes`. The allowlist and every setting are validated at startup.
- **Middleware:** loaded only from the local `middleware.available` registry; configuration selects by name and can never name a module to import. Only install middleware you trust.
- **Rate limiting:** none.

## Reporting a vulnerability

Report privately through [GitHub security advisories](https://github.com/metalayerlab/mitm-proxy/security/advisories/new), never in a public issue.
