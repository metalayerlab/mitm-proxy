# Changelog

## [0.3.6](https://github.com/metalayerlab/mitm-proxy/compare/v0.3.5...v0.3.6) (2026-11-04)


### ✨ Features

* **pipeline:** observe-only streaming middleware (design 0012 step e) ([#41](https://github.com/metalayerlab/mitm-proxy/issues/41)) ([80dde65](https://github.com/metalayerlab/mitm-proxy/commit/80dde658c59fa273da0b3783cd8e6ed706b9c7b1))


### 📚 Documentation

* propose region-aware proxy controller (design 0013) ([#36](https://github.com/metalayerlab/mitm-proxy/issues/36)) ([a74ab64](https://github.com/metalayerlab/mitm-proxy/commit/a74ab64df0fd64b5135d9c9a65ca30af82d6d2ad))

## [0.3.5](https://github.com/metalayerlab/mitm-proxy/compare/v0.3.4...v0.3.5) (2026-11-04)


### ✨ Features

* **upstream:** upstream.read_timeout_ms for slow origins (design 0012 step d) ([#37](https://github.com/metalayerlab/mitm-proxy/issues/37)) ([7bdafa7](https://github.com/metalayerlab/mitm-proxy/commit/7bdafa73813f8f7227a8ea3cc30f9f8931dae97d))

## [0.3.4](https://github.com/metalayerlab/mitm-proxy/compare/v0.3.3...v0.3.4) (2026-11-04)


### ✨ Features

* **middleware:** built-in capture middleware, buffered (design 0012 step c) ([#34](https://github.com/metalayerlab/mitm-proxy/issues/34)) ([dd36168](https://github.com/metalayerlab/mitm-proxy/commit/dd361680f9bb657aaca9182e7b47d73b037ecfbf))

## [0.3.3](https://github.com/metalayerlab/mitm-proxy/compare/v0.3.2...v0.3.3) (2026-11-04)


### ✨ Features

* **proxy:** reverse-proxy mode (design 0012 step b) ([#32](https://github.com/metalayerlab/mitm-proxy/issues/32)) ([df6e981](https://github.com/metalayerlab/mitm-proxy/commit/df6e981cea534ef99a692916bc66a1c1a29d9c65))

## [0.3.2](https://github.com/metalayerlab/mitm-proxy/compare/v0.3.1...v0.3.2) (2026-11-04)


### ✨ Features

* **middleware:** per-middleware configuration (design 0012 step a) ([#30](https://github.com/metalayerlab/mitm-proxy/issues/30)) ([5d6ca44](https://github.com/metalayerlab/mitm-proxy/commit/5d6ca44da2bb9ed7f1854e5be3351f47925070f8))

## [0.3.1](https://github.com/metalayerlab/mitm-proxy/compare/v0.3.0...v0.3.1) (2026-11-03)


### 📚 Documentation

* accept design 0012 capture middleware and reverse-proxy mode ([#27](https://github.com/metalayerlab/mitm-proxy/issues/27)) ([34a1b95](https://github.com/metalayerlab/mitm-proxy/commit/34a1b9551480a5873319a8fa774454652030a566))

## [0.3.0](https://github.com/metalayerlab/mitm-proxy/compare/v0.2.1...v0.3.0) (2026-11-02)


### ⚠ BREAKING CHANGES

* a fail-closed failure on the request side is now 502 "middleware <name> failed" instead of 403, and a middleware with a non-async hook stops startup.

### ✨ Features

* a fail-closed middleware failure is a 502; sync hooks refused at startup (design 0011) ([#17](https://github.com/metalayerlab/mitm-proxy/issues/17)) ([a745161](https://github.com/metalayerlab/mitm-proxy/commit/a7451613337a53fd3a17f84dbf2bb6c308861370))
* a hook that blocks past its timeout, or swallows its cancellation, is now a timeout failure (skipped by default, 502 if fail-closed) instead of having its late result accepted; the log line adds "late: overran" or "late: swallowed" (design 0011) ([bea74b2](https://github.com/metalayerlab/mitm-proxy/commit/bea74b24220242592e72202a08df8c8f42339dd8))


### ♻️ Refactoring

* bump interpose to v0.2.0 and read settings through its typed accessors ([#22](https://github.com/metalayerlab/mitm-proxy/issues/22)) ([579a9ec](https://github.com/metalayerlab/mitm-proxy/commit/579a9ecb5b11311a964485e0e111de9bc395715b))


### 📚 Documentation

* hook timeouts are per call and cancel the hook; hooks must be cancel-safe. ([a745161](https://github.com/metalayerlab/mitm-proxy/commit/a7451613337a53fd3a17f84dbf2bb6c308861370))

## [0.2.1](https://github.com/metalayerlab/mitm-proxy/compare/v0.2.0...v0.2.1) (2026-11-02)


### ✨ Features

* adopt interpose metrics (design 0009) ([#12](https://github.com/metalayerlab/mitm-proxy/issues/12)) ([c3123ae](https://github.com/metalayerlab/mitm-proxy/commit/c3123aee67a91551c2b110ba686f0f1ba3eda41d))
* adopt interpose pipeline core (design 0010) ([#14](https://github.com/metalayerlab/mitm-proxy/issues/14)) ([67be979](https://github.com/metalayerlab/mitm-proxy/commit/67be97909dc75f05dceee199b0bff990b318dde3))

## [0.2.0](https://github.com/metalayerlab/mitm-proxy/compare/v0.1.1...v0.2.0) (2026-11-02)


### ⚠ BREAKING CHANGES

* configuration is now typed strictly. A YAML float for an integer setting (port: 3128.0, max_buffer_bytes: 0.5) and a YAML list or mapping for a text setting are a ConfigError instead of being silently truncated or turned into their repr; an integer written with underscores in an environment variable or _FILE (1_000) is refused; a _FILE value keeps its leading whitespace (only trailing whitespace is stripped).

### ✨ Features

* adopt interpose for settings and secrets ([#10](https://github.com/metalayerlab/mitm-proxy/issues/10)) ([ef5d483](https://github.com/metalayerlab/mitm-proxy/commit/ef5d48313bf346bb13e631d0942157645bfd2d02))

## [0.1.1](https://github.com/metalayerlab/mitm-proxy/compare/v0.1.0...v0.1.1) (2026-11-01)


### 🐛 Bug Fixes

* close the admin socket when bind fails ([6e49a63](https://github.com/metalayerlab/mitm-proxy/commit/6e49a63c13177ad1fb002398495d7273fb6be251))


### 📚 Documentation

* link the project-template issue for this project's template workarounds ([3ef1b36](https://github.com/metalayerlab/mitm-proxy/commit/3ef1b36aa5b9e34d5cd1482b25c7cf30ccd370a7))
* link the project-template issue for this project's template workarounds ([24234c6](https://github.com/metalayerlab/mitm-proxy/commit/24234c629155a11e7df40f3a59d5e722e0b4d643))

## 0.1.0 (2026-10-28)


### ⚠ BREAKING CHANGES

* replace the log middleware with a built-in access log for intercepted and relayed flows

### ✨ Features

* add an opt-in server-timing header to intercepted responses ([f1142c1](https://github.com/metalayerlab/mitm-proxy/commit/f1142c1fc02be422163797dcc5e3cf4bc5ca8772))
* add byte streams, in-memory TLS termination and the raw relay ([78831e7](https://github.com/metalayerlab/mitm-proxy/commit/78831e7eed661cc23b05b996ea7e1850a8df5ffe))
* add flow duration histograms to the metrics endpoint ([0ee4900](https://github.com/metalayerlab/mitm-proxy/commit/0ee49009e27eedf802e3ddb3501651f4b49cffb0))
* add flow timings and the stage breakdown to the flow log line ([0bca4c7](https://github.com/metalayerlab/mitm-proxy/commit/0bca4c7b1cdfef9fa405478d2052f9e9c83b93f8))
* add the explicit proxy source, the intercept-or-relay decision and the proxy server ([98a5c4b](https://github.com/metalayerlab/mitm-proxy/commit/98a5c4b7762d65f3eedb9bbe61dfd6de468ea92c))
* add the http/1.1 and http/2 layers, the upstream pool and the exchange ([0cbc99f](https://github.com/metalayerlab/mitm-proxy/commit/0cbc99ff85fdf3f434532ffc05778ebd2008e61e))
* add the intercepting proxy with middleware, access log and metrics ([04517e6](https://github.com/metalayerlab/mitm-proxy/commit/04517e6d8efa36c90a0cf9b9c4756b9f3879c45b))
* add the middleware pipeline and the built-in flow logger ([5a87746](https://github.com/metalayerlab/mitm-proxy/commit/5a87746f6214f6da87735f66f583ffeebe1198c9))
* add the on_done middleware hook and flow.outcome ([2fa1cfd](https://github.com/metalayerlab/mitm-proxy/commit/2fa1cfd5d836017773d78ec568d7100f81701035))
* add the protocol-agnostic flow model, body streams and framing rules ([77fe893](https://github.com/metalayerlab/mitm-proxy/commit/77fe893a7e58ea19955c5bb978c09e2363f8ecab))
* add the proxy settings and the intercept allowlist ([b39281c](https://github.com/metalayerlab/mitm-proxy/commit/b39281c5d1e17ce03d0c2590dca3bfda798ffb99))
* add the serve, ca and scope commands and the read-only admin api ([cee0c84](https://github.com/metalayerlab/mitm-proxy/commit/cee0c840a62d367da12797762ccd0ea8f0bf62fe))
* parse the TLS ClientHello and manage the root CA ([5c4f95f](https://github.com/metalayerlab/mitm-proxy/commit/5c4f95f9e23dfb68fa0a62d8229010c781a6fced))
* replace the log middleware with a built-in access log for intercepted and relayed flows ([e366590](https://github.com/metalayerlab/mitm-proxy/commit/e3665902be72765aae88135da8d0a783ba3f2608))


### 🐛 Bug Fixes

* apply upstream pool settings from a new configuration, not only at startup ([bb71789](https://github.com/metalayerlab/mitm-proxy/commit/bb717895d06e7c714814be88b8eb7b148d2ff3c7))
* bound closing as well as writing, per write piece, and only on the proxy's own timeout ([dd0c776](https://github.com/metalayerlab/mitm-proxy/commit/dd0c77646ceb9483abbfce95a4ca82894903daa8))
* bound idle http/2 connections and stalled streams by the idle timeout ([3862969](https://github.com/metalayerlab/mitm-proxy/commit/386296993f9cf8a9fa3e7aec76a8633435e79e6f))
* bound socket writes by the idle timeout so a peer that stops reading can't hold a flow ([eea12db](https://github.com/metalayerlab/mitm-proxy/commit/eea12db4f37bd6aef64efd33393fa6dea82d1d83))
* close the upstream websocket socket when switching the client fails ([4a52f7d](https://github.com/metalayerlab/mitm-proxy/commit/4a52f7ddb80fd39a68c73913dc65f1abe8b902c7))
* count applies_to failures in the middleware failure metric ([af661ef](https://github.com/metalayerlab/mitm-proxy/commit/af661ef886e0ca039cfc2a3b12e638d7ac1b768d))
* frame the response middleware leaves, not the one the origin sent ([bb10866](https://github.com/metalayerlab/mitm-proxy/commit/bb108665d240f9dc4e45649ae21272e452cd9cb2))
* give back the http/2 flow-control window of data a finished stream never read ([13b2201](https://github.com/metalayerlab/mitm-proxy/commit/13b22019bb2f3498d98e7347c8936ceb5409b6eb))
* leave open http/2 streams to their own timeouts and blame stalled uploads on the client ([e544fb0](https://github.com/metalayerlab/mitm-proxy/commit/e544fb02c83de1d07f2906b43631ba3f5242683d))
* let open http/2 streams finish after a client's graceful goaway ([4362f91](https://github.com/metalayerlab/mitm-proxy/commit/4362f917d23e0bf0124ded3f8cff281cb906cb70))
* mint a new leaf certificate before a cached one expires ([8937920](https://github.com/metalayerlab/mitm-proxy/commit/8937920c7fbf4aa323d0e7d7b462516549c58bd2))
* stop copying the buffered part of a body that overflows the buffer limit ([37d8033](https://github.com/metalayerlab/mitm-proxy/commit/37d803380f86779d666cf98e36dd4366772ce3eb))
* wrap the project description to the line-length limit ([5fa93d7](https://github.com/metalayerlab/mitm-proxy/commit/5fa93d79b2a4985c630dba1a2eb058cdf8a778aa))


### 📚 Documentation

* add design records 0001 (approved) and 0002 (proposed) ([d718da1](https://github.com/metalayerlab/mitm-proxy/commit/d718da14efbc20a8f5aa34727650f4c18c04c0b0))
* approve design 0003 (flow timings) with its answers and amendments ([9f401f2](https://github.com/metalayerlab/mitm-proxy/commit/9f401f2b7a5fe9a9fa555dcc758db753abd15bf6))
* approve design 0004 (server-timing) with its answers ([22a2866](https://github.com/metalayerlab/mitm-proxy/commit/22a2866bfa45a69e574d0c3c04bba0db2690e923))
* approve design 0007 (flow duration histograms) ([165c188](https://github.com/metalayerlab/mitm-proxy/commit/165c188ee6445c0f6ff11570fcd79a4efc8ce7aa))
* approve designs 0005 (access log) and 0006 (on_done) with their answers ([941f060](https://github.com/metalayerlab/mitm-proxy/commit/941f0609ba309b02a2844a768a255792095fdffb))
* propose a built-in access log (0005) and the on_done hook (0006) ([ea58974](https://github.com/metalayerlab/mitm-proxy/commit/ea58974c63b5266c5e5564c78ac9c6194a1ec545))
* propose flow duration histograms (design 0007) ([aaaf784](https://github.com/metalayerlab/mitm-proxy/commit/aaaf784d76cd554707d8c5ee1b37424e51d6966b))
* propose flow timings (design 0003) ([e82016f](https://github.com/metalayerlab/mitm-proxy/commit/e82016fb44611a92da4723aaf69c4b9388244bb1))
* propose server-timing on intercepted responses (design 0004) ([1dd5bdf](https://github.com/metalayerlab/mitm-proxy/commit/1dd5bdf777ea16a862245d83b7b0d64234e868d9))
* write the wiki for the proxy, its configuration and the middleware api ([30eb224](https://github.com/metalayerlab/mitm-proxy/commit/30eb2246f8a439cce119b2f299a950b0cac471f3))

## Changelog

Generated by [release-please](https://github.com/googleapis/release-please) from
Conventional Commit messages on `main`. Do not edit by hand: change the commit
or PR title instead. See the wiki's Release-Process page.
