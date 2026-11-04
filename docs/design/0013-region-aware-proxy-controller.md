# 0013: A region-aware proxy controller

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Proposed
Date: 2026-11-04
Authors: Stephen Cox; drafted by Claude (Claude Code)

## Context

### The need

An operator sends web traffic through several outbound VPN exits, each in a different country, and some
sites refuse it by region. Today the exit for each site is chosen by hand, in a browser proxy extension,
and a block is noticed only by reading the page. The operator wants one proxy controller, built on
mitm-proxy, that:

1. **keeps a log of the sites that block by region**: what was blocked, when, through which exit, and
   what happened next;
2. **enforces the routing rules it is given**: each site goes through the exit its rule names;
3. **routes for real**: the site sees the exit's country for its pages, its content servers, its licence
   server and its name lookups alike;
4. **fails closed**: when a rule's exit is down, the request fails with a clear error. It never falls back
   to a direct connection, which would reveal the client's real location.

The words used below, once:

- An **exit** is an outbound proxy that leaves the network through a VPN tunnel (or Tor) in one country.
- A **rule** matches a site and names the exits to use for it, in order.
- A **tunnelled** site is relayed as raw bytes after the client's `CONNECT` request: the proxy sees only
  the host name and port, never the content. An **inspected** (intercepted) site is decrypted by
  mitm-proxy with its own certificate authority, so its responses can be read.
- A **signature** is a named, testable pattern that recognises a region-block response.
- **Failing closed** means refusing the request; **failing open** would mean letting it through some other
  way.

### What mitm-proxy has today

From the design records (by number) and the code at release 0.3.4:

- **A forward proxy.** Clients are configured to use it; `CONNECT` requests are tunnelled (0001).
- **Selective decryption.** Only the hosts in the allowlist `intercept.hosts` are decrypted (exact names or
  `*.suffix`, no regular expressions, no lone `*`). Every other host is relayed untouched (0001).
  `mitm-proxy scope check HOST` says which.
- **Middleware.** Named async hooks (`applies_to`, `on_request`, `on_response`, chunk hooks) run on
  decrypted flows only; tunnels get none (0001). A middleware that fails is skipped (fail open), unless
  it declares `on_error = "fail"`, in which case the flow is answered `502` (0010, 0011).
- **`on_done`.** One observe-only hook per flow after it ends, however it ended, with a read-only
  `flow.outcome` (`origin`, `local`, `denied`, `error`, `aborted`, `websocket`) (0006).
- **The access log, timings and metrics.** A built-in access log for decrypted flows and relayed tunnels
  (0005), per-stage timings (0003), Server-Timing (0004), Prometheus histograms (0007), all through
  interpose's metrics (0009).
- **Reverse mode and `flow.direction`.** A plain-HTTP reverse listener to one fixed upstream; every flow
  carries `direction` (`inbound` or `egress`) (0012).
- **Per-middleware options.** `middleware.config.<name>` passed to the constructor; secrets inside written
  as `{secret_file: …}` leaves (0012). These come from interpose's **section settings** and **typed list
  items** (interpose v0.4.x, its design record 0009), which mitm-proxy already pins (v0.4.1) and uses for
  `reverse`.
- **The capture middleware** (0012, buffered, in release 0.3.4): redacted JSONL records, one file per
  rule per UTC day, `0600` files in a `0700` directory, written from `on_done`.
- **A connector seam.** Every upstream connection is opened by a `Connector` (`upstream/pool.py`), which
  today resolves the origin's name itself and connects straight to it.

What it does not have: **upstream proxies (chaining) and SOCKS** are in 0001's "Explicitly out of scope"
list. So today mitm-proxy cannot send anything through an exit.

### An example deployment

The design is generic, but it is easiest to follow against one deployment. This record uses this
example throughout:

- **Exits in Kubernetes.** Two VPN exits, `exit-a` and `exit-b`, each an HTTP proxy in its own pod (for
  example [gluetun](https://github.com/qdm12/gluetun), whose built-in HTTP proxy supports `CONNECT`),
  and a Tor exit, `exit-tor`, a SOCKS5 proxy. Each has its own Service in a namespace `exits`, and a client
  picks an exit **by port**: one port per exit, no header, no authentication on the exits themselves.
- **Countries.** `exit-a` leaves in country `XA` and `exit-b` in `XB` (codes from ISO 3166's user-assigned
  range, used here as placeholders).
- **Each VPN exit has a kill switch:** if its tunnel drops, requests fail instead of leaving by the pod's
  own network.
- **Name lookups go through the exit.** A VPN exit resolves names inside its tunnel for the requests it
  proxies; the Tor exit is used as SOCKS5 with remote name resolution (`socks5h`). There is no separate
  DNS port.
- **Readiness.** Each exit pod's readiness probe makes a real request through its own proxy (for example
  `https://www.gstatic.com/generate_204`, or a Tor check page for the Tor exit). An exit whose probe fails
  drops out of its Service's ready endpoints, without being restarted. Latency is not published anywhere.
- **Hazards worth designing for.** One pod per exit, so restarting it drops connections in flight; a VPN
  exit can stay not-ready for minutes after a restart, so clients should fail over on not-ready; the
  exits have no authentication, so whatever sits in front of them must be reachable only by its intended
  clients.
- **One client:** a browser with a proxy extension, per-site patterns on some exits and a default exit for
  everything else. The controller replaces those patterns with one proxy entry.

### Constraints

- **Fail closed on routing; fail open on observation.** A routing failure refuses the request. A failure
  in detection, the block log or learning never breaks traffic (0001, 0010).
- **No new dependency.** HTTP `CONNECT` chaining, SOCKS5, the Kubernetes API read and the pattern matcher
  are standard library code.
- **Throughput.** 0001 said "throughput is not a design driver". For a router that carries 4K video it is,
  on the tunnelled path. That is why the first step is a throughput gate.
- **Decided-against lists bind** (TODO.md): no regular expressions in host patterns, no priority numbers,
  no retry on middleware failure.

## Decision

### 1. What it is, and its name

**A mitm-proxy feature, deployed as its own instance.** Routing through exits is a core feature of
mitm-proxy (settings, connectors, the exchange). Region-block detection, the block log and learning are
built-in components that only run when configured. "The controller" is one mitm-proxy instance, from the
same image, with these settings on.

Why a feature and not a separate program that uses mitm-proxy: routing must decide the upstream of
**tunnels**, which no middleware sees, and a retry must re-send a request through a different upstream
connection. Both live in the exchange and the connection pool. A separate program would have to reach
into those internals or fork them. As a feature, mitm-proxy keeps one place for TLS, the pool, the access
log and metrics, and the controller costs nothing when `exits` is empty (the default).

**Name: `passport`** (recommended), for the deployment profile and the documentation ("the passport
profile"): each site is shown the right passport at the border. Alternatives: **`waypoint`** (neutral,
but says nothing about regions) and **`border`** (says regions, but reads as blocking rather than
routing). The settings use plain names (`exits`, `routing.*`, `regions.*`), so the name is only a label.
Open question 1.

### 2. Exits and chaining

A new list setting `exits`, one item per exit:

```yaml
exits:
  - name: exit-a
    url: http://exit-a.exits:8001       # an HTTP proxy: CONNECT for TLS, absolute form for plain HTTP
    country: XA
  - name: exit-b
    url: http://exit-b.exits:8002
    country: XB
  - name: exit-tor
    url: socks5h://exit-tor.exits:9050  # SOCKS5, names resolved by the exit
    retry: false
```

| Field | Type | Default | Secret | Meaning |
|---|---|---|---|---|
| `name` | str | *(required)* | no | Unique; lower-case letters, digits and `-`. Names a metric label value, so the set is fixed by configuration (0009). `direct` is reserved. |
| `url` | str | *(required)* | no | `http://host:port` or `socks5h://host:port`. No path, no user info (a password in a URL is a `ConfigError`: it would be a secret in a plain setting). |
| `country` | str | `""` | no | An ISO 3166 code, for display and the block log only. |
| `health` | str | `""` | no | The Kubernetes Service whose readiness is this exit's health. Empty: the host part of `url` up to the first dot (`exit-a`). |
| `retry` | bool | `true` | no | May a blocked page load be retried through this exit (section 7)? |
| `auth` | str | `""` | **yes** | `user:password` for the exit's proxy, as a `{secret_file: …}` leaf or `MITM_PROXY_EXITS_<N>_AUTH_FILE`. For exits that require a login; the example's exits don't. |

**Connectors.** Two new `Connector`s beside `TCPConnector`, chosen by the exit's scheme:

- **`HTTPConnectConnector`** opens TCP to the exit, sends `CONNECT host:port HTTP/1.1` with `Host`
  (and `Proxy-Authorization` when `auth` is set), and expects a `2xx`. For a decrypted flow it then runs
  TLS to the origin inside that tunnel, verified as always (0001), with the origin's name as SNI. A plain
  `http://` request goes to the exit in absolute form instead.
- **`Socks5Connector`** speaks SOCKS5 (RFC 1928): no authentication, or username and password (RFC 1929)
  when `auth` is set, and a `CONNECT` with the **domain-name address type**, so the exit resolves the name
  (the meaning of `socks5h`).

**The controller never resolves a routed site's name.** It resolves only the exit's own name (a cluster
Service), and passes the site's name to the exit. With exits like the example's, every lookup is then
made inside the exit's tunnel. An IP literal is passed as it is.

**The pool** keys connections by exit as well as by host, port and SNI (`Target` gains `via`), so a
connection opened through one exit is never reused through another.

**`direct`** is a reserved exit name meaning today's behaviour (resolve and connect from the controller).
It may be named in a rule only when `routing.allow_direct` is true (default false). It is never used as a
fallback, whatever the settings.

**Routing is on when `exits` is not empty.** With `exits` empty, mitm-proxy behaves exactly as today.

### 3. Rules

```yaml
routing:
  default_via: [exit-b]                 # sites no rule matches
  rules:
    - match: "*.example-news.org"
      via: [exit-a]
    - match: "example-news.org"
      via: [exit-a]
    - match: "shop.example/{country}/{rest*}"
      via: [exit-a, exit-b]
      inspect: true
      note: "prices differ by country"
```

| Setting | Type | Default | Meaning |
|---|---|---|---|
| `routing.default_via` | list | *(none)* | The exits, in order, for a site no rule matches. Empty while `exits` is set: an unmatched site is refused (`503`, section 9). |
| `routing.rules` | list of sections | *(none)* | Manual rules, the operator's: fields below. From the config file or `MITM_PROXY_ROUTING_RULES_FILE` (a YAML list, for a ConfigMap mount). |
| `routing.learned_rules` | list of sections | *(none)* | Learned rules the operator has reviewed (section 8): the same fields plus `proposal` (its ID), `answer` (`accept` or `reject`) and `answered` (a date). Only `accept` items route; a `reject` item only stops the same proposal coming back. From `MITM_PROXY_ROUTING_LEARNED_RULES_FILE`, kept apart from the manual file. |
| `routing.allow_direct` | bool | `false` | Allow the reserved exit `direct` in rules. |

Rule fields:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `match` | str | *(required)* | A host pattern, optionally followed by a path pattern (below). |
| `via` | list | *(none)* | Exit names, in order: the first ready one is used. Required unless `service` is set. |
| `service` | str | `""` | The name of a service (section 6) whose exits and stickiness apply. Exclusive with `via`. |
| `inspect` | bool | `false` | Decrypt this site (section 4). |
| `note` | str | `""` | Why the rule exists; shown by `mitm-proxy routes` and in `/v1/routes`. |

**The pattern language reuses sluice's router.** sluice (a sibling metalayerlab project, a routed FUSE
filesystem) has an Express-style router: patterns of literal segments, `{name}` for exactly one segment and
a final `{name*}` for zero or more, and **specificity ranking**: a literal beats `{name}`, which beats
`{name*}`, compared segment by segment, and two patterns of the same shape are a load error (sluice's
design record 0001). Here a pattern has two levels:

- **The host level** is 0001's allowlist syntax, kept exactly: `example.org`, or `*.example.org` for any
  subdomain at any depth (not the bare name). No regex, no lone `*`. Internally the labels are reversed
  and matched with sluice's segment matcher (`*.example.org` becomes `org`, `example`, `{label}`,
  `{more*}`), so specificity is the same rule: an exact host beats a suffix, and a longer suffix beats a
  shorter one.
- **The path level** (optional, after the first `/`) is sluice's path syntax: `/watch/{id}`,
  `/{country}/{rest*}`. The `{…*}` capture is last within its level.

The host level is compared first; the path level breaks ties within one host. Captured parameters are
only shown by the debugging commands; nothing renders them.

**What is shared, and where it lives.** Shared: the segment parser, the matcher and the specificity
comparator, and the two debugging verbs' shape (sluice's `routes` and `resolve`). Not shared: sluice's
registration API in Python (here the rules are settings, which sluice's design rejected for its own
routes because its routes carry code), middleware attachment and backend templates. **Copied first**, into
`src/mitm_proxy/routing/pattern.py`, with a header naming sluice's file and commit. **Then moved to
interpose** by its own interpose design record once both copies are stable. Why not interpose first:
interpose couples both projects' releases, so the router should prove itself in one project before the
other depends on it; the move is then a small change with two users to test it.

**Manual rules always win.** The order of lookup is:

1. the **manual** rules (`routing.rules`, and the hosts of `routing.services`), most specific first;
2. only if no manual rule matches, the **learned** rules the operator accepted;
3. otherwise `routing.default_via`.

A learned rule never overrides a manual one, however specific. A host listed both in a service and in a
manual rule is a `ConfigError`.

**Tunnelled sites are routed by host only.** For a tunnel the controller sees only `CONNECT host:port`, so
rules with a path level must have `inspect: true`; one without it is a `ConfigError` ("a path can only be
matched on a decrypted site").

**Commands.**

| Command | What it does | Exit codes |
|---|---|---|
| `mitm-proxy routes [-c FILE]` | The rule tree in match order: manual, learned, default; each with its exits, service, `inspect` and note. | 0, 2 |
| `mitm-proxy route check URL [-c FILE]` | A dry run for one URL or `host:port`: the matching rule and its source, the exits in order, the service, whether it would be decrypted. Like `scope check`. | 0 decrypted, 1 tunnelled, 2 error, 3 refused (no rule, no default) |

### 4. Two paths: inspected and tunnelled

- **Inspected:** a site whose rule has `inspect: true`, or whose host is in a service's `inspect` list
  (section 6), is decrypted, so its responses can be checked for region blocks (section 5). It still
  leaves through its rule's exit.
- **Tunnelled:** every other site is relayed straight to its exit after `CONNECT`, never decrypted, as
  today's unlisted hosts are.

**How it relates to `intercept.hosts`.** The effective allowlist is the union of `intercept.hosts` and the
hosts of every inspecting rule and service. `intercept.hosts` keeps its meaning (decrypt, for any
middleware) and is not a routing setting; `inspect` is the routing way to say the same thing, so a rule and
its decryption are written together. An inspecting rule whose host level couldn't be an allowlist entry
can't happen, because the host level *is* allowlist syntax. `mitm-proxy scope check HOST` names the entry
that matched, and now also the rule ("rule `shop.example/{country}/{rest*}`, inspect").

Inspection is kept to the sites where detection matters: main pages and licence servers, not the content
servers that carry video, which stay tunnelled for speed.

### 5. Region-block detection

Detection runs in the exchange on **inspected flows only**, after the origin's response head arrives and
before response middleware. It is part of the core, not a middleware, because it must be able to discard
the response and send the request again through another exit (section 7), which only the exchange can do,
and because a "retry" result in the middleware chain would sit next to TODO.md's decided-against "kill or
retry the flow on middleware failure". A detector that raises is logged and treated as "no block" (fail
open).

**Which responses are examined.** A response is a **candidate** when its status is in any enabled
signature's `status` list. Only candidates are held: up to `regions.body_window_bytes` of the body
(default 64 KiB, decoded first if `gzip` or `deflate`, with stdlib `zlib`; other encodings are matched on
headers only). Every other response streams through untouched, so a 200 video manifest pays nothing.

**A signature** is a named list item; every condition given must hold (and), and within a list any value
may match (or):

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | str | *(required)* | Unique; lower-case, digits and `-`. A metric label value. |
| `confidence` | str | `high` | `high`: a match is a block and may trigger a retry. `low`: logged as `suspected`, never retried, never learned from. |
| `status` | list | *(required)* | Response statuses. |
| `scope` | str | `any` | `any` inspected host, `service` (hosts of any service) or `licence` (a service's `licence_hosts` only). |
| `headers` | list | *(none)* | `name: substring` pairs, all of which must be present (names and values compared without case). |
| `location` | list | *(none)* | Globs on a redirect's `Location` (host and path, no query): `*` within a segment, `**` across. |
| `body` | list | *(none)* | Phrases, any of which must appear in the held body window (compared without case, whitespace collapsed). |
| `max_body_bytes` | int | `0` | If set, only bodies whose full length (from `Content-Length`, or the held window ending the body) is at most this match: block pages are small, articles about blocking are not. |

**The built-in signatures**, each shipped with tests:

| Name | Matches | Confidence | Keeping false positives down |
|---|---|---|---|
| `http-451` | Status `451` (RFC 7725, "Unavailable For Legal Reasons") | high | The status exists for this. |
| `cloudflare-country-ban` | `403`, `server: cloudflare`, body phrase "error code: 1009" or "has banned the country or region" | high | Cloudflare's own error number for a country ban; its other 403s (firewall rules, bots) don't carry it. |
| `region-text-403` | `403` (and `401`) with a region phrase in the body: "not available in your country", "not available in your region", "unavailable in your country", "not available in your location", and the like | high | A `403` already means refused; the phrase says why. |
| `region-text-200` | `200`, `text/html`, a region phrase as above, `max_body_bytes: 32768` | low | Many sites answer a block with a small `200` page, but so does an article about geo-blocking: only small pages, and logged as suspected. |
| `redirect-unavailable` | `301`, `302`, `303`, `307`, `308` with `Location` on `**/unavailable**`, `**/not-available**`, `**/geo*block**`, `**/region*restrict**` | low | Generic paths can mislead; a service's own `unavailable_redirects` (section 6) give a high-confidence form. |
| `akamai-access-denied` | `403`, `server: AkamaiGHost`, body "Access Denied" and "Reference #" | low | The same page is used for bot defence, so only suspected. |
| `licence-denied` | `403` or `451` from a host in a service's `licence_hosts` | high, `scope: licence` | Only on hosts the operator named as licence servers, where a refusal is almost always a region decision. |

A video player's licence request is a binary `POST`, so a licence failure is logged but never retried
(section 7); its effect is to count against the exit for that service (section 8).

**Settings:**

| Setting | Type | Default | Meaning |
|---|---|---|---|
| `regions.enabled` | bool | `false` | Run detection on inspected flows. |
| `regions.signatures` | list of sections | *(none)* | More signatures, or a built-in replaced by giving its name. |
| `regions.disable` | list | *(none)* | Built-in signatures to turn off, by name. |
| `regions.body_window_bytes` | int | `65536` | How much of a candidate's body is held and searched. |
| `regions.annotate` | bool | `true` | Add `Proxy-Status` (RFC 9209) to a blocked or retried response: the signature, the exit and the retry, never the client's data. |

**Adding a signature.** In configuration, as above, for one deployment. To ship it, a PR that adds it to
`src/mitm_proxy/regions/signatures.py` with at least one matching and one near-miss fixture under
`tests/fixtures/regions/<name>/` (a minimal, made-up response: status, headers and only the phrase that
matters, never a copied page). Two commands help:

| Command | What it does | Exit codes |
|---|---|---|
| `mitm-proxy regions list [-c FILE]` | Every enabled signature with its conditions and where it came from (built in, configured, replaced). | 0, 2 |
| `mitm-proxy regions check FILE [--host HOST] [-c FILE]` | Runs the signatures against a saved HTTP response (status line, headers, body) as if from `HOST`, and names the ones that match. | 0 matched, 1 none, 2 error |

### 6. Services

A **service** is a named group of hosts that must share one exit: a site's main pages, its content
servers and its licence server. A video service that sees its pages from one country and its licence
request from another refuses to play.

```yaml
routing:
  services:
    - name: example-tv
      hosts: [example-tv.com, "*.example-tv.com", "*.example-cdn.net"]
      inspect: [example-tv.com, licence.example-tv.com]
      licence_hosts: [licence.example-tv.com]
      unavailable_redirects: ["example-tv.com/**/not-in-your-region**"]
      via: [exit-a, exit-b]
```

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | str | *(required)* | Unique, lower-case. Names the stickiness key and log entries. |
| `hosts` | list | *(required)* | Host patterns (allowlist syntax). Each acts as a manual rule naming this service. |
| `inspect` | list | *(none)* | The subset of `hosts` to decrypt. |
| `licence_hosts` | list | *(none)* | The subset of `hosts` that are licence servers (the `licence` signature scope). |
| `unavailable_redirects` | list | *(none)* | `Location` globs that are this service's "not available" page: a high-confidence signature scoped to it. |
| `via` | list | *(required)* | Exits, in order. |
| `sticky` | bool | `true` | Keep the whole service on one exit (below). |

**Stickiness.** A service's flows stick to one exit so that a session is never split across countries:

- **Key:** the service's name (and, for a rule with more than one exit but no service, the rule's
  pattern). Not the client address by default: behind a Kubernetes LoadBalancer with
  `externalTrafficPolicy: Cluster`, the address the controller sees may be a node's rather than the
  client's. `routing.sticky_key: client` adds the client address to the key, for a deployment where
  addresses are preserved and there are several clients.
- **Binding:** the first flow binds the key to the first ready exit in `via`. Every flow refreshes it.
- **How long:** until `routing.sticky_idle_ms` without a flow (default 30 minutes) or
  `routing.sticky_max_ms` in all (default 12 hours), whichever is first, or until its exit goes down
  (open question 2).
- **When the exit goes down:** the binding ends; flows already open on it end with it (restarting a
  one-pod exit drops them anyway); the next flow binds to the next ready exit in `via`, and the change is
  logged.
- **After a confirmed block** with a successful retry (section 7), the binding moves to the exit that
  worked, for the rest of the binding's life. This is temporary, shown in `/v1/routes`, and never
  written into the rules: it is not a learned rule.
- Bindings live in memory. A restart starts fresh, which costs one rebinding.

**Name lookups for a service** happen inside its exit, because every host in the service goes through the
same exit and the controller never resolves a routed name (section 2). So a content network that steers by
the resolver's location sees the same country as the site.

| Setting | Type | Default | Meaning |
|---|---|---|---|
| `routing.services` | list of sections | *(none)* | Fields above; also from `MITM_PROXY_ROUTING_SERVICES_FILE`. |
| `routing.sticky_idle_ms` | int | `1800000` | Idle time that ends a binding. |
| `routing.sticky_max_ms` | int | `43200000` | Longest life of a binding. |
| `routing.sticky_key` | str | `service` | `service` or `client`. |

### 7. Retry once, for a simple page load

A **simple page load** is a request that is safe and idempotent (RFC 9110, section 9.2) and small enough
to hold:

- the method is `GET` or `HEAD`;
- there is no request body (no `Content-Length` above 0, no `Transfer-Encoding`);
- no `Range` and no `Upgrade` header (not a media segment, not a WebSocket);
- it is a document load: `Sec-Fetch-Dest: document`, or, without that header, an `Accept` that lists
  `text/html` first;
- nothing of the response has reached the client yet (true for a held candidate, section 5).

When a **high-confidence** signature matches a simple page load, the exchange discards the response,
closes that upstream connection, and sends the same request (as it stood after `on_request` middleware,
which doesn't run again) **once** through the next ready exit in the rule's `via` list that has
`retry: true`. The second answer goes to the client, whatever it is: if it is blocked too, the client
sees the real block page. There is never a third attempt.

- A rule with **one exit** is never retried: a manual choice of one exit stays the operator's choice.
  The block is logged as `not-retried:pinned`.
- Anything other than a simple page load is logged and not retried (`not-retried:unsafe`).
- `regions.retry` (bool, default `true`) turns retrying off and leaves detection and the log on.

### 8. The block log, and learning

**The block log.** One JSON object per line, one entry per detected block, in
`<regions.log_dir>/blocks-YYYY-MM-DD.jsonl` (the UTC date; default `log_dir` is `<state_dir>/blocks`),
written from the flow's end like the capture records (0012): files `0600` in a `0700` directory, one write
per line under a per-file lock, off the event loop, fail open with a rate-limited WARNING and a counter.

```json
{"v": 1, "time": "2026-11-04T10:15:02.123Z", "site": "example-tv", "host": "example-tv.com",
 "exit": "exit-b", "signature": "cloudflare-country-ban", "confidence": "high", "status": 403,
 "request": "simple", "outcome": "retried-ok", "retry_exit": "exit-a", "flow": "9eba5076…"}
```

(Shown wrapped; on disk it is one line.)

| Field | Meaning |
|---|---|
| `time` | When the blocked response arrived, UTC. |
| `site` | The service's name if the host is in one, otherwise the host. |
| `host` | The host only: no path, no query, no port unless it isn't 443. |
| `exit`, `retry_exit` | The exit used, and the one retried through (or `null`). |
| `signature`, `confidence`, `status` | What matched, and the response status. |
| `request` | `simple`, `unsafe` or `licence`. |
| `outcome` | `retried-ok`, `retried-blocked`, `no-exit-left`, `not-retried:pinned`, `not-retried:unsafe`, `suspected` (a low-confidence match), `retry-off`. |
| `flow` | The flow's ID, to join with the access log. |

**What it never holds:** paths, query strings, headers, cookies, bodies or any page content, and the
client's address. The site and host are the only browsing data in it. Retention:
`regions.log_retain_days` (default 90) removes whole day files older than that, once a day; `0` keeps
everything. Anything that reads the log through `/v1/blocks` (section 11) gets the same fields and no
more.

**Learning.** Per site and exit, the controller counts, over `learning.window_days` (default 14):
`blocked` (high-confidence blocks, including licence denials) and `ok` (simple page loads on an inspected
host answered below `400` with no signature matched). The counts are saved to
`<state_dir>/learning/stats.json` every 10 minutes and on shutdown, so they survive a restart.

A **proposal** is made for a site when all of these hold:

- no manual rule matches it;
- its current exit has at least `learning.min_blocks` blocks (default 3) on at least
  `learning.min_days` distinct days (default 2);
- one other exit has at least `learning.min_ok` successes (default 3), from retries;
- no reviewed learned rule for the same site and exit exists (a rejected proposal never comes back).

A proposal's `match` is the blocked host exactly, or the service's hosts for a service; the reviewer may
widen it. A proposal changes nothing. It is shown at `GET /v1/proposals`:

```json
{"id": "p-3f2a…", "site": "example-news.org", "match": "example-news.org", "propose": ["exit-a"],
 "current": ["exit-b"], "evidence": {"exit-b": {"blocked": 5, "ok": 0, "days": 3},
 "exit-a": {"blocked": 0, "ok": 4}}, "signatures": ["region-text-403"],
 "first_seen": "2026-11-02T08:01:00Z", "last_seen": "2026-11-04T10:15:02Z"}
```

**Proposed, not applied.** Learned rules are never applied silently: a wrong one would quietly move a site
to another country. The interface is two things, both plain data:

1. **Out:** `GET /v1/proposals`, the open proposals with their evidence (counts, days, signatures; never
   paths or content).
2. **In:** the learned-rules file (`routing.learned_rules`), one item per reviewed proposal, with its
   `proposal` ID, `answer` and `answered` date.

Between them sits **an operator's review queue**, whatever the deployment uses: a ticket, a pull request
to the configuration, a note to answer. It reads the proposals, asks a person, and writes the answer into
the learned-rules file (or, for "make it a manual rule", into the manual rules). The controller picks the
file up on restart (mitm-proxy has no reload trigger yet), and an accepted rule applies.

| Setting | Type | Default | Meaning |
|---|---|---|---|
| `regions.retry` | bool | `true` | Section 7. |
| `regions.log_dir` | str | *(none)* | Empty: `<state_dir>/blocks`. |
| `regions.log_retain_days` | int | `90` | Whole day files older than this are removed; `0` keeps all. |
| `learning.enabled` | bool | `true` | Count and propose (only with `regions.enabled`). |
| `learning.window_days` | int | `14` | How far back counts reach. |
| `learning.min_blocks` | int | `3` | Blocks on the current exit before proposing. |
| `learning.min_days` | int | `2` | Distinct days those blocks span. |
| `learning.min_ok` | int | `3` | Successes on the proposed exit. |

### 9. Failing closed

**Health.** An exit is **up** when both hold:

1. **Kubernetes says it is ready.** The controller watches the EndpointSlices of each exit's `health`
   Service in `health.namespace` (default: its own namespace, from the service-account files), with a
   read-only service account (`get`, `list` and `watch` on `endpointslices` in that namespace, nothing
   else). An exit is ready when its Service has at least one ready endpoint: that is the exit pod's
   readiness probe, a real request through its own proxy. The watch gives changes within a second or so;
   a full list every `health.resync_ms` (default 30 s) catches a missed event.
2. **Its own probe passes.** Every `health.probe_interval_ms` (default 30 s) the controller makes one
   request through each exit to `health.probe_url` (default `https://www.gstatic.com/generate_204`), with
   `health.probe_timeout_ms` (default 10 s). It records the connect time and time to first byte as the
   exit's latency, which nothing else publishes. After `health.probe_failures` failures in a row
   (default 3) the exit is down until a probe passes.

`health.source: probe` uses the probes alone, for exits outside Kubernetes.

**When the health source is unreachable.** If the Kubernetes API can't be read, the last known readiness
is kept for `health.stale_after_ms` (default 2 minutes). After that the probes alone decide, and
`/v1/status`, `/metrics` and the log say the health source is stale. If the probes fail as well, the exit
is down. Not knowing is never "up". `/readyz` does not depend on the API, so an API outage doesn't take
the controller itself out of service (0002 rejected the same coupling for the control plane).

**Failover** is within the rule's `via` list only, in order, to an exit that is up; never to `direct` and
never to an exit the rule doesn't name (a region-sensitive site sent through the wrong country is also a
wrong answer). That covers an exit that stays not-ready for minutes after a restart. A connection to an
exit that fails before any byte of the request has gone also moves to the next exit in the list; after
that, the request fails.

**When nothing in the list is up**, or no rule matches and `routing.default_via` is empty:

| Path | The client gets |
|---|---|
| Tunnelled (`CONNECT`) | `503 Service Unavailable`, `Retry-After: 30`, `Proxy-Status: mitm-proxy; error=destination_unavailable; details="exit exit-a down"`. Browsers show their own "proxy refused" page for a failed `CONNECT`. |
| Inspected | A local `503` HTML page: the site, the rule, the exits it may use and since when each is down, and that nothing was sent to the site. The flow's outcome is `local`, by `routing`. |
| Plain HTTP | The same page as inspected. |

Every such refusal is logged at INFO (the site and exit, never the path), counted in
`mitm_proxy_route_refused_total{exit, reason}`, and never forwarded anywhere.

**A second line of defence.** The controller's network policy should admit egress only to the exits'
Services and the Kubernetes API, so even a fault in this code cannot reach the internet directly.
Deployment says so; if a deployment allows `direct`, its policy must allow that too, and Security will
say what that costs.

| Setting | Type | Default | Meaning |
|---|---|---|---|
| `health.source` | str | `kubernetes` | `kubernetes` (readiness and probes) or `probe` (probes only). |
| `health.namespace` | str | *(none)* | Where the exits' Services are. Empty: the pod's own namespace. |
| `health.resync_ms` | int | `30000` | Full re-read of readiness. |
| `health.stale_after_ms` | int | `120000` | How long the last known readiness is trusted when the API can't be read. |
| `health.probe_url` | str | `https://www.gstatic.com/generate_204` | What each probe fetches through the exit. |
| `health.probe_interval_ms` | int | `30000` | Time between probes, per exit. |
| `health.probe_timeout_ms` | int | `10000` | A probe slower than this has failed. |
| `health.probe_failures` | int | `3` | Failures in a row that make an exit down. |

(`health.*`, not `exits.*`: interpose doesn't allow a section setting's key to be the parent of another
setting's.)

### 10. The first gate: streaming throughput

Before anything else is built past the minimum it needs, the tunnelled path must show that one asyncio
process can carry 4K video through an exit. The minimum is step 1 below: `exits`, the two connectors and
`routing.default_via`, because mitm-proxy can't reach an exit at all without them.

**How it runs.** A script, `benchmarks/throughput_gate.py` (standard library), run in a scratch pod in
the exits' namespace with two containers: mitm-proxy with one exit as its default, and a client. Against
the same exit, alternating, five rounds of each:

- **through mitm-proxy:** `curl -x http://127.0.0.1:3128` downloading a large file over HTTPS from a
  public speed-test endpoint, for 2 minutes, logging bytes each second;
- **baseline:** the same download straight to the exit (`curl -x` the exit's Service);
- **a real stream:** one 4K video format fetched through each path with `yt-dlp --proxy`, to see a video
  content network's real rate;
- **latency:** 200 small requests (the probe URL) through each path, timing connect and first byte.

It writes the per-second samples and a summary to its output directory, and prints pass or fail.

**The pass mark** (open question 3):

- **Sustained:** through mitm-proxy, the median of the per-second rates is at least 95% of the
  baseline's, and the 10th percentile is at least 25 Mbit/s (4K streaming services ask for about 15 to
  25 Mbit/s). Also with two downloads at once, each at least 25 Mbit/s.
- **Added latency:** first byte through mitm-proxy minus the baseline: at most 5 ms at the median and
  15 ms at the 95th percentile.
- **Cost:** mitm-proxy under 0.5 of a CPU core at that rate.
- **Inconclusive** if the baseline itself is under 25 Mbit/s: the exit is the bottleneck, so the gate is
  run again on another exit.

A person's own check, 10 minutes of 4K playback in a browser without rebuffering, comes at rollout, once
the controller is reachable from the client; it confirms the gate, it isn't the gate.

**If mitm-proxy can't keep up: sing-box.** sing-box is a fast proxy router (in Go) with domain rules, HTTP
and SOCKS outbounds and per-outbound DNS. Then:

- sing-box takes the client's connections and routes the **tunnelled** sites to the exits, from rules
  generated from the same settings (`mitm-proxy routes --export sing-box`), with no `direct` outbound, so
  it fails closed too;
- it sends the **inspected** sites to mitm-proxy, which keeps detection, the block log, retry, learning,
  services' stickiness for those hosts, and its own chaining to the exits;
- lost or weakened: stickiness across a service's tunnelled hosts (sing-box would need its own), the
  health model on the tunnelled path (sing-box's own checks), and one place for metrics.

Everything in sections 3 to 8 survives as written for the inspected path; sections 2 and 9 survive for
mitm-proxy's own upstreams.

### 11. Observability

**The admin API** (read-only, on `listen.port`, as 0002 says it stays) gains:

| Method and path | Response |
|---|---|
| `GET /v1/exits` | Each exit: name, kind (`http` or `socks5h`), country, `up`, Kubernetes readiness and its age, the last probes, latency (median and 95th percentile of the last 20), `since`. |
| `GET /v1/routes` | Manual rules, learned rules, services, the default, and the current sticky bindings with their ages. `?url=` gives `route check`'s answer as JSON. |
| `GET /v1/blocks` | Block log entries, newest first; `?since=` (ISO time) and `?limit=` (default 500). |
| `GET /v1/proposals` | Open proposals (section 8). |

`/v1/status` gains a `routing` object: whether routing is on, a hash of the rules, how many exits are up,
and the health source's state. These new endpoints carry host names by design, unlike `/v1/status` (0002:
"no secrets, no hostnames"), so the admin port must be reachable only by the operator (section 12).

**Metrics,** with every label's values declared from configuration (0009), and no site or host label:

| Metric | Labels |
|---|---|
| `mitm_proxy_route_flows_total` | `exit`, `path` (`tunnel`, `inspect`, `plain`) |
| `mitm_proxy_route_refused_total` | `exit`, `reason` (`down`, `no_rule`, `connect`) |
| `mitm_proxy_route_failover_total` | `from_exit`, `to_exit` |
| `mitm_proxy_exit_up` (gauge) | `exit` |
| `mitm_proxy_exit_probe_seconds` (histogram) | `exit` |
| `mitm_proxy_region_blocks_total` | `signature`, `exit`, `outcome` |
| `mitm_proxy_region_log_records_total` | `result` (`written`, `failed`) |
| `mitm_proxy_health_source_stale` (gauge) | none |

The access log (0005) gains `exit=<name>` at the end of each line, and middleware get a read-only
`flow.route` (the exit name, the rule's pattern and its source), so `capture` and other middleware can see
where a flow went.

**CLI verbs** (section 3 and 5): `routes`, `route check`, `regions list` and `regions check` read the
configuration offline; the `/v1` endpoints give the live state.

**An optional external status command.** Anything outside the controller (a status-bar light, a
monitoring check, an operator's own front-door command) can read `/v1/exits`, `/v1/blocks` and
`/v1/proposals` and turn them into states: all exits up, a failover happened, requests refused, health
source stale, proposals waiting. Such a command is the deployment's, not this repository's; the
endpoints' JSON is its interface. A command that changes routing (an exit picker) would need a write
endpoint, which the admin API doesn't have (0002 keeps it read-only): that is not in this record.

### 12. Deployment

Deployment is the operator's. What the controller expects, using the example deployment:

- **Where:** a Deployment of the mitm-proxy image beside the exits (in their namespace keeps one network
  policy), one replica, `strategy: Recreate` (stickiness and learning are per process).
- **Reaching the exits:** by their in-cluster Service names, not an external address; the exits' network
  policies must admit the controller's pods.
- **Its own port:** one port for the client's proxy entry, reachable only by the intended clients (source
  ranges and a firewall), never open to the rest of the network, since the exits behind it have no
  authentication. The admin port the same way, or reached through the Kubernetes API's service proxy.
- **Egress policy:** only to the exits' Services and the Kubernetes API (section 9).
- **Configuration:** a ConfigMap with the settings file, and the rules, services and learned rules as
  separate files (`MITM_PROXY_ROUTING_RULES_FILE` and the others), so a rules change doesn't touch the
  rest.
- **Secrets** the controller needs, as files, never environment variables (interpose's `_FILE` and
  `{secret_file: …}` leaves): its CA certificate and key (`MITM_PROXY_CA_CERT_FILE`,
  `MITM_PROXY_CA_KEY_FILE`, a root made for this controller alone), its proxy password
  (`MITM_PROXY_PROXY_AUTH_FILE`; recommended, open question 4), and exit passwords only for exits that
  need them. The VPN providers' logins stay with the exits: the controller chains to them and never needs
  those.
- **State:** a small volume at `state_dir` for the learning counts and the block log.
- **A read-only service account** for the readiness watch.

Deployment questions (names, namespaces, ports, firewall rules, secret storage, where the rules files are
kept and who edits them) are the operator's.

## Build order

Each step is one PR, useful alone. "This repo" is mitm-proxy; "the deployment" is the operator's own
configuration and manifests.

| # | Step | Where | Useful alone because |
|---|---|---|---|
| 1 | **Exits and chaining:** `exits`, the HTTP `CONNECT` and SOCKS5 connectors, `routing.default_via`, the pool keyed by exit, no local resolution; `benchmarks/throughput_gate.py`. | this repo | mitm-proxy can chain through any exit; the gate can run. Needed even if sing-box takes over (section 10). |
| 2 | **Gate test pod:** a scratch pod in the exits' namespace, admitted to one exit by the policy. | the deployment | Nothing else needs to change. |
| 3 | **Run the gate**, record the numbers, decide: carry on, or sing-box. | this repo (the results in its PR) | Decides the rest. |
| 4 | **Rules:** the pattern matcher copied from sluice, `routing.rules`, specificity, manual before default, `routes` and `route check`. | this repo | Per-site patterns move out of the browser into one place. |
| 5 | **Health and failing closed:** the readiness watch, probes, failover in `via`, the local `503`s, `/v1/exits`. | this repo | The core promise: never direct. |
| 6 | **The controller's deployment:** network policies, service account, ConfigMap, secrets, volume, its own port. | the deployment | The browser needs one proxy entry; rollback is its old per-exit entries. |
| 7 | **Two paths:** `inspect` on rules, the union with `intercept.hosts`, `scope check` naming the rule; the CA trusted by the client. | this repo; the deployment for the CA | Decryption only where it is asked for. |
| 8 | **Services and stickiness.** | this repo | Video services stop splitting across countries. |
| 9 | **Detection and the block log** (no retry): signatures, fixtures, `regions` commands, `/v1/blocks`, metrics. | this repo | The block log exists. |
| 10 | **Retry once** for simple page loads, `Proxy-Status`, sticky rebinding. | this repo | Blocked pages fix themselves. |
| 11 | **Learning and proposals:** counts, `/v1/proposals`, `routing.learned_rules`. | this repo | Patterns surface without hunting for them. |
| 12 | **A review queue and a status command** reading the `/v1` endpoints. | the deployment | Proposals get answered; the state is visible outside the controller. |

## Alternatives considered

- **sing-box as the router from the start.** Fast and mature, with domain rules and per-outbound DNS. It
  can't decrypt and inspect responses, so detection, the block log and learning would need mitm-proxy
  anyway, and its rules would be a second configuration language beside interpose's. Kept as the fallback
  if the gate fails.
- **Routing by browser profile or extension only (proxy-extension patterns).** No block log, no
  detection, no retry, no learning; patterns live in one browser's storage; a service's hosts must be
  listed by hand on the same exit; and health is invisible until a page fails.
- **A separate DNS resolver per exit.** Exits like the example's already resolve inside their tunnels,
  and with SOCKS the name goes to the exit. A resolver would be one more service that could disagree with
  the exit about where a name points.
- **Applying learned rules automatically.** A false positive could quietly move a site (a bank, say) to
  another country, and nobody would know why it changed.
- **Failing open** (going direct when an exit is down). It reveals the client's real location to the very
  sites it is routed away from, which defeats the controller.
- **Detection as a middleware with a "retry" result.** Middleware never see tunnels, and a retry action in
  the chain sits beside the decided-against "kill or retry the flow on middleware failure". The exchange
  owns the upstream, so the retry is the exchange's.
- **A separate program that wraps mitm-proxy.** It would need the pool, the connectors and the exchange's
  internals; a feature keeps one TLS stack, one pool, one log.
- **Regular expressions or priority numbers for rules.** Both decided against in 0001 and TODO.md;
  specificity ranking gives a predictable order without numbers.
- **Grouping hosts by registrable domain (the public suffix list).** A new dependency and data to keep
  current; services name the groups that matter, and say which are licence servers.
- **Sticky by client address.** Behind a LoadBalancer that rewrites source addresses it is meaningless,
  so it is only an option for deployments that preserve them.

## Consequences

**Security.**

- **Where decryption happens:** in the controller's pod, only for the inspected sites (the union of
  `intercept.hosts` and the inspecting rules and services, which `scope check` and `/v1/routes` show).
  Everything else is relayed without being read.
- **The CA's trust:** the client trusts the controller's root for every site, so whoever holds its key
  can impersonate any site to that client. The key is a file mounted read-only (`0400`), made for this
  controller alone (not shared with any other mitm-proxy instance), and never in an environment variable.
  Two mitigations are recommended for the build: X.509 name constraints on the root limited to the
  inspected hosts, if the client honours them on a root it trusts (to be tested in step 7), and keeping
  the inspected list short.
- **VPN credentials:** untouched; they stay with the exits.
- **The controller's own port** is an open proxy to every exit if exposed: source ranges plus
  `proxy.auth`.
- **What the log holds:** time, site, host, exit, signature, status and outcome, for blocked requests only.
  That is browsing data for those sites, so: no paths, queries, headers, bodies or client addresses, files
  `0600`, a 90-day default retention, and `/v1/blocks` only where the admin port is restricted.
- **Not covered:** traffic that bypasses a proxy in the client (WebRTC, QUIC). Through an HTTP proxy a
  browser uses TCP, as 0001 notes for QUIC.

**Changes to earlier records.**

- This amends 0001's out-of-scope list: "Upstream proxies (chaining), SOCKS" loses chaining and SOCKS
  towards an upstream (SOCKS from clients stays out). 0001's "throughput is not a design driver" no
  longer holds for tunnels routed through exits. The allowlist gains a second source (`inspect`), and
  "an unlisted host is never touched" still holds.
- 0002: the new endpoints are read-only, but carry host names, unlike `/v1/status`. `exits`, `routing.*`
  and `regions.*` are managed settings when managed mode is built; `health.*` is bootstrap.
- 0005: the access log gains `exit=`.
- 0012: unchanged. `capture` can run on the controller (with `flow.route` to see the exit), and exit
  passwords use its `{secret_file: …}` leaves.

**Rollout and rollback.** Every setting is off by default (`exits` empty), so each step changes nothing
until configured. The controller runs beside the exits, which keep their own ports: the client switches
to the controller's one entry, and rolls back by switching to the old per-exit entries. Each step reverts
alone.

**Wiki pages** it will touch: In-Depth-Configuration (`exits`, `routing.*`, `health.*`, `regions.*`,
`learning.*`), CLI-Reference (`routes`, `route check`, `regions list`, `regions check`),
HTTP-API-Reference (the four endpoints, the metrics), Architecture-Internals (connectors, the routing
step, detection and retry in the exchange), Security (decryption scope, the CA, the log, exposure),
Deployment (the controller profile, its service account and policies), Troubleshooting (the `503`s,
`Proxy-Status`, stale health), Writing-Middleware (`flow.route`), Testing-Guide (signature fixtures),
Quick-Start (a two-exit example), and the example configuration.

**TODO.md:** the rejected alternatives go into "Decided against".

## Open questions

Deployment questions are the operator's (section 12). These are about the design:

1. **The name.** `passport`, `waypoint` or `border`? Recommended: **`passport`**, because it says what the
   controller does (show each site the right country); the settings don't depend on it.
2. **The stickiness period, and rebinding after a retry.** 30 minutes idle and 12 hours at most, with a
   binding moving to the exit a successful retry used. Recommended as is: long enough for a film with a
   pause, short enough that a changed rule applies the same day; and without rebinding, every page of a
   blocked service pays a block and a retry. The rebinding is shown and expires, and never becomes a rule.
3. **The throughput pass mark.** Recommended: **as in section 10** (95% of the exit's own rate, 10th
   percentile 25 Mbit/s, two streams, 5 ms added at the median, 15 ms at the 95th percentile, half a
   core). The numbers lean on the streaming services' published guidance; tighter would test the exits
   rather than mitm-proxy.
4. **A password on the controller's port.** Recommended: **yes** (`proxy.auth` from a secret file):
   unlike the exits, the controller decrypts traffic, so its port deserves more than source ranges.
