# 0009: Adopt interpose metrics

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Implemented
Date: 2026-11-02
Authors: Stephen Cox; drafted by Claude (Claude Code)

## Context

`metrics.py` writes its own Prometheus text exposition (0001, 0007): `Counter`, `Gauge` and a classic
`Histogram`, with the rule that keeps the series count bounded ("every label has a small, fixed set of
values: the intercepted host is never a label") stated in a comment. Nothing enforced it.

[interpose](https://github.com/metalayerlab/interpose) v0.1.1 ships the same types (its design 0002,
"Metrics"), with the rule enforced: a label is a `Label` with a declared set of values, and a value
outside the set is a `ValueError` at the call that used it, naming the metric and label, never the
value. Its "Consumer adoption" section is the brief for this record.

Constraints:

- **`/metrics` stays byte-for-byte the same** for the same operations: the dashboards and recording rules
  built on 0007 depend on it.
- No new dependency beyond the interpose tag bump (stdlib only underneath).
- Out of scope: the pipeline core (interpose design 0003), which is the next record.

## Decision

1. **Pin:** `interpose @ git+https://github.com/metalayerlab/interpose@v0.1.2`, still the inline PEP 508
   form, first in `dependencies` (0008). v0.1.2 is the tag after the metrics one; it also carries the
   pipeline core, which this record doesn't use yet.
2. **`metrics.py`** keeps `Metrics`, `NETWORK_BUCKETS`, `MIDDLEWARE_BUCKETS`, `observe_flow`, `all()`
   and `render()`, and drops its own `_escape`, `_format`, `Metric`, `Counter`, `Gauge`, `Histogram` and
   `_le` for `interpose.metrics`. The metrics are registered in a `Registry` in the same order as before,
   and `render()` is `Registry.render()`. `api.py` answers with `interpose.metrics.CONTENT_TYPE` (the
   same string as the old `PROMETHEUS_TEXT`).
3. **Every label's values are declared up front:**

   | Label | Values | From |
   |---|---|---|
   | `version` | the running version | `Metrics(version)` |
   | `type` | `http`, `websocket`, `plain_http` | `FlowType`, plus `http.flow.PLAIN_HTTP` (was a literal in `exchange.py`) |
   | `reason` | `intercept`, `out_of_scope`, `not_tls`, `sni_out_of_scope` | `classify.Decision` |
   | `direction` | `request`, `response` | |
   | `protocol` | `http/1.1`, `h2` | what `ConnectionPool.counts()` reports (not `h1`, as the brief guessed) |
   | `result` | `applied` | the only result `config_applies` counts |
   | `kind` | `ca` | |
   | `outcome` | `origin`, `local`, `denied`, `error`, `aborted`, `websocket` | `http.flow.OUTCOME_KINDS`, new beside `Outcome` |
   | `reused` | `true`, `false` | |
   | `middleware` | the registry's names | see 4 |

4. **The `middleware` label** is one shared `Label`, `Metrics.middleware`, used by both
   `mitm_proxy_middleware_failures_total` and `mitm_proxy_middleware_duration_seconds`.
   `ProxyServer._build` declares every name in `middleware.available` (not only the chain), plus the names
   of middleware passed in directly, before it builds the pipeline: at startup and on every `apply()`.
   Names reach the metrics only from there, never from traffic. Declared names are never removed, so a
   series keeps rendering after a reload drops its middleware, as before.
5. **Tests:** `tests/unit/metrics_golden.txt` is the full output of every metric, rendered by the old
   exposition (v0.2.0) from a fixed set of calls; `test_metrics.py` renders the same calls through
   interpose and compares bytes. The escaping test declares its odd name (`'we"ird\\name\n'`) first. New
   tests: every value the proxy uses is declared, an undeclared one is refused without being named, one
   declaration covers both middleware metrics, and the registry is declared at build and on reload. The
   tests of the generic types that stay are the golden histogram strings, on interpose's classes.

### What changes

**`/metrics` output: nothing.** The golden test is the proof. interpose escapes `\` and newline in HELP
text, which mitm-proxy didn't; no help string contains either.

Behaviour, none of it reachable from traffic or configuration:

| Case | Before | Now |
|---|---|---|
| A label value outside its declared set (a code bug) | a new series | `ValueError` at the call |
| The wrong number of label values (a code bug) | an error when `/metrics` rendered | `ValueError` at the call |
| A negative or non-finite counter increment, a histogram `NaN` | accepted | `ValueError` |
| A gauge holding `inf` or `NaN` | `/metrics` failed (`OverflowError`) | renders `+Inf`, `-Inf`, `NaN` |

The first row is the point: a host, path or user name can't become a label by accident. Every metric
call the proxy makes uses a value from the table above, and the tests exercise each one.

## Alternatives considered

- **Keep the hand-written exposition.** Two copies (here and interpose) would drift, and the bounded-label
  rule would stay a comment.
- **Declare only `middleware.chain`.** Only chained names can be counted, but declaring the whole
  registry costs nothing (a declared value has no series until it is used) and is what interpose's brief
  asks for.
- **Pre-register every label combination at zero** (Prometheus's advice for known label sets). It would
  add series that render as `0` today and change the output; rejected (also in interpose 0002).
- **`prometheus_client`:** already rejected in 0007.

## Consequences

- The bounded-labels rule is enforced; a missed declaration fails a test the first time its code path
  runs, not Prometheus's memory.
- A new metric or label value in mitm-proxy declares its values in `metrics.py`; a test that counts a
  middleware name declares it first (`bench().context()` does it for its chain, as `_build` does).
- Rollback: revert the PR; nothing in deployments or dashboards changes.
- Wiki: HTTP-API-Reference (the declared values per label), Architecture-Internals (`metrics.py`'s role,
  where the text comes from), Testing-Guide (the golden file).

## Open questions

1. ~~**Status:** written and implemented in the same PR.~~ Decided 2026-11-02: approved under Stephen's
   overnight delegation (merge with `--admin`) and marked Implemented at merge; Stephen to confirm on
   review.
