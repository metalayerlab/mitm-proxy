# TODO

Short-lived working list. Bugs and features with discussion belong in GitHub issues; this file is for what
the team (and its AI assistants) should know at a glance.

## Open

- [x] Write docs/design/0001-initial-design.md (docs/ai/prompts.md, prompt 1)
- [x] Replace every `<!-- guidance -->` comment in docs/wiki/ (a Release PR is blocked until then)
- [ ] project-template: `scripts/gen-wiki.py` skips Typer sub-groups (`ca init`, `ca show`, `scope check` are documented by hand in CLI-Reference)
- [ ] project-template: the Dockerfile skeleton `EXPOSE`s only 8080; the proxy also listens on 3128
- [ ] Design 0002 (Proposed): answer its open questions; the control-plane client, enrollment and managed mode are not built

## Decided against (don't re-propose without reading this first)

<!-- One line each: the idea, why not, and a link to the issue, PR or design record. For example:
- Helm chart: kustomize base + Flux overlays cover our clusters; a chart doubles the maintenance (design 0003).
-->
- Blanket intercept-all or a denylist for scope: fails open, breaks pinned apps (design 0001).
- Regex allowlist entries: easy to get subtly wrong where it matters most; exact + `*.suffix` only (design 0001).
- Any option to disable upstream TLS verification: silent total downgrade; use `upstream.extra_ca_file` (design 0001).
- Buffered-only middleware hooks (`on_response(flow, body)`): can't be upgraded to streaming (design 0001).
- Runtime mutation detection + re-buffer for body framing: degrades silently to full buffering (design 0001).
- Custom backpressure/windowing layer: `drain()` and h2 flow control already do it (design 0001).
- Priority-number or DAG middleware ordering: undebuggable ordering dependencies (design 0001).
- Kill or retry the flow on middleware failure: too disruptive / undefined after side effects (design 0001).
- Fresh upstream connection per flow: handshake per request (design 0001).
- Hand-rolled HTTP/1.1 or HTTP/2 framing: use h11 / h2 (design 0001).
- Per-leaf RSA keys: too slow for in-process minting; one shared ECDSA leaf key (design 0001).
- Proxy on 8080 with the admin API moved: breaks the template's health checks for a habit (design 0001).
- One-label `*.suffix` allowlist matching: "everything under" is what lab users mean (design 0001).
- Refusing oversized bodies: breaks large downloads; they stream through uninspected, logged (design 0001).
- Resetting the stream on response DENY: opaque to debug; 502 naming the middleware (design 0001).
- FlowLogger on by default: it would buffer every intercepted flow (design 0001).
- Control plane pushing to an inbound REST API on each instance: needs an exposed authenticated port, fails behind NAT (design 0002).
- gRPC/xDS management: heavy dependency, undebuggable, far beyond a lab tool's needs (design 0002).
- Kubernetes operator/CRDs as the control plane: k8s-only (design 0002).
- Middleware code or module paths in managed config: remote code execution; names from a local registry only (design 0002).
- Secret hashes in reported effective config: brute-forceable; report `{"set": bool}` (design 0002).
- `/readyz` depending on control-plane reachability: an outage would drain the whole fleet (design 0002).
- Automatic re-enrollment after 401/403: single-use token loops; revoked stays revoked (design 0002).
- Running `on_response` for flows middleware answered itself (STOP/DENY): changes what STOP means; cache and rewrite middleware would act on local answers (design 0005).
- Calling `on_done` from background tasks: hooks are never `create_task`ed (design 0006).
