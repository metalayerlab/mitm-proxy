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

**What the proxy reveals to clients:** by default, only what interception needs (its certificates, and its own error responses: `403`, `407`, `502`, `503`, `504`; a routing refusal's `503` names the site and why, never an exit's address; a middleware's deny or fail-closed failure names that middleware in the body, and nothing else). The access log (`proxy.access_log`) records each intercepted request's URL, including its query string, which can hold tokens; treat the log as sensitive. `proxy.access_log_relayed` also records every destination host and client address the proxy relays for, so it's off by default. With `proxy.server_timing` on, every intercepted response also carries the proxy's timings, and middleware names when middleware answered. A page's own scripts can read them (`PerformanceResourceTiming.serverTiming`) and report them to the site, so a site can tell it was intercepted. The proxy never sends `Timing-Allow-Origin`, so other sites' scripts can't read them. Turn it on deliberately, for debugging.

**Captures** (the built-in `capture` middleware) hold request and response bodies, which can be personal or secret data beyond what redaction catches: credentials in a body that isn't JSON, in a header you didn't list, or in a server-sent event's line that isn't a JSON `data:` line. Capture is opt-in per rule, and nothing is recorded until a rule matches. Files are created `0600` in a directory created `0700`, for the proxy's user only; redaction happens before anything is written, never afterwards. Keep the directory on storage only that user reads, and delete old files yourself: capture never does. See [In-Depth Configuration](In-Depth-Configuration#capture).

**Exits** (design 0013, step 1). With `exits` set, every upstream connection goes through an exit and nothing ever falls back to a direct connection: an exit that can't be reached fails the request, and with no exit to use the request is refused with a `503` and sent nowhere. `direct` is used only when `routing.allow_direct` allows it and `routing.default_via` names it. When routing is on, mitm-proxy resolves only the exits' own names, never a site's, so the site's name lookup happens where the exit is; an IP literal is passed on as it is. Origins are still verified end to end inside an exit's tunnel: an exit can see which host and port you connect to (and, for plain `http://`, the whole request), but not the content of a TLS connection. An exit's login (`auth`) is a secret, sent only to that exit; a URL with user info is refused, so a password can't sit in a plain setting. An exit with no login of its own is open to whoever can reach it, and so is this proxy in front of it: give the proxy `proxy.auth` and keep its port reachable only by its intended clients. A code fault is still a risk to "never direct": a deployment's own egress policy that admits only the exits is the second line of defence.

**Out of scope:**

- clients that choose to trust the root;
- what your middleware does with the plaintext;
- denial of service (there's no rate limiting). Stalled clients are bounded, though: a request whose client stops sending or reading ends after `proxy.idle_timeout_ms`, and an idle connection with no request in progress is then closed, so a stalled client can't hold resources forever.

## Secrets

- Secrets are read from files only (`MITM_PROXY_<KEY>_FILE`), never from the config file. See [In-Depth Configuration](In-Depth-Configuration#secrets).
- Values are never logged. Logs and `mitm-proxy config` show where a value came from, never the value itself.
- `proxy.auth`, `ca.key` and each exit's `auth` are held as `Secret` values: printing, logging or formatting one gives `********`, and an invalid value's error message leaves it out. Code reads the value only with `reveal()`, at the point of use.
- CI scans every commit with gitleaks, and secrets in history fail the build.

| Secret | Grants | Rotate |
|---|---|---|
| `ca.key` (or `<state_dir>/ca/ca-key.pem`) | Impersonating any site to clients that trust the root | `mitm-proxy ca init --force` (or a new Secret), restart, then remove the old root from every client and install the new one. Remove the old root even if you don't rotate, once you stop using the proxy. |
| `proxy.auth` | Using the proxy | Change the value in its secret file and restart the proxy; update clients. |
| `exits[N].auth` | Using that exit, as the proxy does | Change the login at the exit and in its secret file, then restart the proxy. |

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
- **Reverse listeners:** a `reverse` listener has no authentication — whoever reaches its bind address reaches its one upstream. The bind address is its only guard, so bind it where only the clients you mean can reach it, and never expose it wider than that upstream should be. It forwards only to the upstream you configured, never anywhere else.
- **Input:** HTTP/1.1 is parsed by h11 and HTTP/2 by h2, both strict; request heads are limited to 64 KiB, peeked ClientHellos to 64 KiB, bodies buffered for middleware to `body.max_buffer_bytes`. The allowlist and every setting are validated at startup.
- **Middleware:** loaded only from the local `middleware.available` registry; configuration selects by name and can never name a module to import. Only install middleware you trust. A middleware fails open by default (a crash skips it for the flow); one that enforces a policy should set `on_error = "fail"`, so its failure ends the flow with `502 middleware <name> failed` instead of letting it through. That body names the middleware only, never the exception or a value from the flow; a `403` is always a deliberate deny. A hook that isn't `async def` is refused at startup (see [Writing Middleware](Writing-Middleware#failing-closed-and-your-own-timeout)).
- **Rate limiting:** none.

## Reporting a vulnerability

Report privately through [GitHub security advisories](https://github.com/metalayerlab/mitm-proxy/security/advisories/new), never in a public issue.
