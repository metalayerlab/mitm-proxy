# 0010: Adopt interpose pipeline core

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Implemented
Date: 2026-11-02
Authors: Stephen Cox; drafted by Claude (Claude Code)

## Context

`pipeline.py` has its own loop and guard: select with `applies_to`, run each hook in chain order under
`middleware.hook_timeout_ms`, time it into `flow.timings.middleware`, and on any failure log it, count it
and skip that middleware for the rest of the flow (fail open, 0001). sluice runs the same loop with the
opposite failure policy. [interpose](https://github.com/metalayerlab/interpose) v0.1.2 ships the shared
mechanism (its design 0003, "Pipeline core"): a `Pipeline` with a project `Policy`, a per-unit `Run`, one
guard around every hook, and per-middleware `on_error` and `timeout`. Its "Consumer adoption" section is
the brief for this record, and its "Decisions for Stephen to confirm" list is answered below.

The point of adopting it is per-middleware failure policy: today a crashing blocklist is skipped, so the
traffic it should stop gets through.

Constraints:

- **No observable change by default.** Every middleware that doesn't opt in behaves as today: the
  statuses, the outcome, the log lines (two edge cases aside, below), the metrics and the timings.
- `Middleware`, `Action`, `Outcome`, `load_middleware`, `Pipeline` and `FlowPipeline`'s public methods
  and attributes stay, so `exchange.py`'s phase calls and the existing tests don't change.
- The pin is already v0.1.2 (0009); no new dependency.

## Decision

1. **Policy:** `pipeline.policy(hook_timeout)` returns
   `Policy(on_error="skip", after_order="chain", after_local=False, observe_failed=False, timeout=hook_timeout)`:
   fail open, response hooks in registration order and never after a local answer (0005), `on_done`
   never reaches a middleware that failed (0006), and the timeout from `middleware.hook_timeout_ms`
   (5 s by default).
2. **Entries:** `Pipeline.register(name, mw)` keeps its checks (non-empty, unique, a `Middleware`) and
   builds `interpose.pipeline.Entry.of(mw, name=name)`: the name is always the registry key, and the
   middleware's own `on_error` and `timeout` class attributes set its policy. `Middleware` declares both
   as `None` (the project's). `NO_LIMIT` and `HookFailed` are re-exported from `mitm_proxy.pipeline`. A
   wrong value for either attribute is a `StartupError` from `load_middleware` naming the middleware (a
   `ValueError` from `register`).
3. **`FlowPipeline`** keeps `run_request`, `run_request_chunk`, `run_response`, `run_response_chunk`,
   `run_done`, `applies`, `mutation_capable`, `request_skipped`, `response_skipped`, `failed`, `entries`
   and `on_failure`, as thin wrappers over a `Run` started with
   `select=applies_to`, `timings=flow.timings.middleware` and `state=flow.metadata`:
   - `run_request`: `before("on_request", ...)` until a result that isn't `None` or `CONTINUE`; a
     result that isn't an `Action` or `None` raises an `InvalidResult` inside the guard (the same check
     as interpose's `valid`, done in the call so the log can name the type); a `Stopped` becomes
     `Outcome(action, by=name)`.
   - `run_response`: `each("on_response", ...)` over `after_pairs()`, so `after_local=False` is what
     keeps `on_response` off local answers, not only `exchange.py`'s early return.
   - chunk hooks: `fold(..., keep_none=True)`; each call converts the result to `bytes` (a `bytearray` or
     `memoryview` is accepted, as today), and anything else is an `InvalidResult` inside the guard.
     `bytes(3)` would be three zero bytes, so the check is by type, not by conversion.
   - `run_done`: `observe("on_done", ...)`: the selected entries minus the failed ones; always fail open.
   - `on_failure` (the interpose callback) logs with the flow ID, in today's wording, and calls the
     metrics counter with the middleware's name.
4. **`HookFailed` (only for a middleware with `on_error = "fail"`):** answered as a `DENY` by that
   middleware in the phase it failed in, with today's statuses and body:
   - from `applies_to` (`start()`): the flow is denied before any hook runs. Nothing was selected, so no
     middleware gets `on_done` for it. A body over `body.max_buffer_bytes` is denied too: a gate that
     couldn't decide doesn't let an uninspected body through.
   - from `on_request`: `run_request` returns `Outcome(DENY, by)`, so the exchange sends
     `403 denied by middleware <name>`;
   - from `on_request_chunk`: the exchange catches it and sends the same `403`;
   - from `on_response` or `on_response_chunk`: `502 denied by middleware <name>`, as a response `DENY`.

   The outcome is `denied:<name>`, the failure is logged and counted as before, and `on_done` runs for
   the middleware that applied and haven't failed.
5. **Audit of `name`, `on_error` and `timeout` attributes:** mitm-proxy ships no middleware (the `log`
   middleware went in 0005); the test middleware and the wiki's examples use none of the three names.
   `name` is ignored anyway (the registry key wins). Writing-Middleware now lists `on_error` and `timeout`
   as reserved.

### Decisions in interpose 0003 that touch mitm-proxy

Each answered with the option that keeps today's behaviour:

| 0003 decision | Here |
|---|---|
| 1. No defaults for the four semantics fields | Written out in `policy()`, each as today |
| 2. `"fail"` raises `HookFailed`; 0003 recommends a 502 naming the middleware in both phases | **Not adopted.** The phase's existing deny: 403 on the request side, 502 on the response side (see [Open for Stephen](#open-for-stephen)) |
| 3. Observer hooks always fail open | Kept: `on_done` can't deny, whatever `on_error` says |
| 5. Sync-hook rejection is opt-in (`require_async`) | Not adopted: a sync hook still fails at run time and is skipped, not a startup error |
| 6. `skip_inherited` is opt-in | Off: every hook is called, so timings keep every middleware |
| 8. State isn't renamed | `flow.metadata` is passed as `state`; `scoped()` isn't used |
| 9. Names needn't be unique in the core | The registry's uniqueness check stays in `register` |
| 11. A `valid` refusal is a failure | Kept: today's "not an Action" / "not bytes or None" rule |

### What changes

| | Before | Now |
|---|---|---|
| An invalid `on_request`/`on_response` result in the log | its `repr`: `returned 'deny' from on_request` | its type name: `returned 'str' from on_request` (never log values; chunk hooks already logged the type) |
| A hook that raises `TimeoutError` itself (its own network call) | logged as `timed out … after 5.0s` | logged as `failed in …` with its traceback: only the hook timeout counts as a timeout (interpose's guard). Skipped and counted the same |
| Hook timing clock | `time.monotonic()` | `time.perf_counter()` (only differences are used; the values are the same kind) |
| A middleware with an `on_error` or `timeout` class attribute | ignored | its failure policy, or a startup error if the value is wrong |
| A middleware with `on_error = "fail"` that fails | n/a | the flow is denied by it (decision 4) |
| `FlowPipeline(...)`, `pipeline.Entry` | `FlowPipeline(flow, hook_timeout)`; `Entry(name, middleware, mutation_capable)` | built by `Pipeline.start()`; `Entry` gains `core` (internal; nothing else constructs either) |

Nothing else a client, a log reader or Prometheus sees changes: the existing pipeline, exchange and
`on_done` tests pass unchanged.

## Alternatives considered

- **Keep the hand-written runner.** Two loops (here and interpose) drift, and per-middleware policy would
  be written twice.
- **Let `HookFailed` from the phases reach `exchange.py`.** It would duplicate the deny code for each
  phase. `run_request`/`run_response` already return an `Outcome`, so a failure under `"fail"` becomes the
  `DENY` the exchange already handles; only the chunk hooks, which return bytes, raise.
- **Run `on_done` for the chain after a fail-closed `applies_to` failure.** interpose's `start()` raises
  before selection finishes, so there is no run whose entries could be told; selecting by hand would
  re-implement the guard.

## Consequences

- One loop and one guard for mitm-proxy and sluice; per-middleware `on_error` and `timeout` are available
  now.
- A gate can fail closed; its crash denies the flow instead of letting it through.
- Rollback: revert the PR; no setting, status or metric changes for middleware that don't opt in.
- Wiki: Writing-Middleware (failing closed, the reserved attributes), Architecture-Internals
  (`pipeline.py`'s role, the timing clock, the failure rule), Troubleshooting (the log lines, the
  fail-closed deny), Security (gates should fail closed), Testing-Guide (`test_failure_policy.py`).

## Open questions

### Open for Stephen

Each is implemented the way that keeps current behaviour; each has a recommendation.

1. **A fail-closed failure's status.** interpose 0003 recommends `502 middleware <name> failed`, in both
   phases, so a crash stays distinguishable from a deliberate request-phase `403`. Implemented instead:
   the phase's existing deny (403 request, 502 response, `denied by middleware <name>`). Recommendation:
   adopt the 502 in a follow-up if operators need to tell a crash from a deny without reading the log
   (the log and `mitm_proxy_middleware_failures_total` already tell them apart).
2. **`on_error` and `timeout` from configuration** (say `middleware.<name>.on_error`). Today only the
   middleware's class sets them. Recommendation: wait until someone needs to override a third-party
   middleware's choice; it is a new setting and needs its own record.
3. **Sync hooks at startup** (`require_async`). Recommendation: adopt; a sync hook can never work, and a
   startup error beats a failure on every flow. It changes behaviour, so not here.
4. **Status:** written and implemented in the same PR. Approved under Stephen's overnight delegation
   (merge with `--admin`) and marked Implemented at merge; Stephen to confirm on review.
