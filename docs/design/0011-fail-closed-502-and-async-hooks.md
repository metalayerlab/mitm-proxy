# 0011: A 502 for a fail-closed failure, and async hooks checked at startup

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Implemented (approved by Stephen 2026-11-02)
Date: 2026-11-02
Authors: Stephen Cox; drafted by Claude (Claude Code)

## Context

0010 adopted interpose's pipeline core and left three questions open for Stephen. He answered two on
2026-11-02 (decisions D2 and D5); this record carries them out. It supersedes 0010's decision 4 (the
statuses of a fail-closed failure) and its "not adopted" row for interpose 0003's decision 5 (`require_async`).
The rest of 0010 stands.

- **D2:** under 0010, a middleware with `on_error = "fail"` that failed was answered like its deliberate
  deny in that phase: `403 denied by middleware <name>` on the request side, `502` with the same text on
  the response side. A client, or an operator reading only the status, can't tell a crash from a policy
  decision.
- **D5:** a hook written as a plain `def` can never work (the pipeline awaits it), but under 0010 it only
  failed when a flow first called it, on every flow, and was skipped.

## Decision

1. **A fail-closed failure is a 502 naming the middleware, in both phases.** When a middleware with
   `on_error = "fail"` fails (`HookFailed`) in `applies_to` (raised from `Pipeline.start()`), `on_request`,
   `on_request_chunk`, `on_response` or `on_response_chunk`, the client gets
   `502` with the body `middleware <name> failed`. The body holds the middleware's name and nothing else:
   never a value from the flow or the exception's text (both stay in the log).
   - `pipeline.Outcome` gains `failed: bool = False`. `FlowPipeline.run_request`/`run_response` return
     `Outcome(DENY, name, failed=True)` for a `HookFailed`; the exchange builds the same outcome when a
     chunk hook raises `HookFailed`.
   - A **deliberate** `DENY` is unchanged: `403 denied by middleware <name>` on the request,
     `502 denied by middleware <name>` on the response, whatever `on_error` says.
   - The flow's outcome stays `denied:<name>` (status `502`), so `on_done`, the access log and the flow
     histograms' `outcome` label keep their values. The failure is logged and counted in
     `mitm_proxy_middleware_failures_total` as before, and the exchange's info line adds `(it failed)`.
2. **Sync hooks are refused when middleware is registered.** `Pipeline.register` (through
   `_core_entry`) calls interpose's `require_async(middleware, ASYNC_HOOKS, name=name)` for
   `on_request`, `on_request_chunk`, `on_response`, `on_response_chunk` and `on_done`. `applies_to` stays a
   plain call. A sync hook is a `TypeError` from `register`, and a `StartupError` from `load_middleware`:
   `middleware '<name>': <hook> must be an async function`.
   - **Startup:** `ProxyServer` builds its pipeline through `load_middleware`, so the CLI prints the line
     and exits 2, like any other middleware error.
   - **Reload** (`ProxyServer.apply`): matches how it handles every configuration error today. `_build`
     raises before the new snapshot is assigned, so `apply` raises the `StartupError`, the running
     snapshot (its pipeline included) stays, and `mitm_proxy_config_applies_total` isn't incremented.
3. **Hook timeouts, documented** (no code change): `middleware.hook_timeout_ms` (or a middleware's
   `timeout`) applies to each hook call, every chunk-hook call included, not to the flow. A timeout
   cancels the hook at its current `await`, so hooks must be cancel-safe (cleanup in `try`/`finally` or
   `async with`) and must never swallow `CancelledError`. Blocking or CPU-bound work goes off the event
   loop (`asyncio.to_thread`): a timeout can't interrupt synchronous code, and a thread started that way
   keeps running after the timeout.

### What changes

| | Before (0010) | Now |
|---|---|---|
| Fail-closed failure in `applies_to`, `on_request`, `on_request_chunk` | `403 denied by middleware <name>` | `502 middleware <name> failed` |
| Fail-closed failure in `on_response`, `on_response_chunk` | `502 denied by middleware <name>` | `502 middleware <name> failed` |
| Deliberate `DENY` | `403`/`502 denied by middleware <name>` | unchanged |
| A hook written as a plain `def` | fails on every flow, skipped (fail open) | refused at startup, or the reload is refused and the old pipeline stays |

This is a breaking change for clients or monitors that read a `403` as "a fail-closed middleware broke",
and for third-party middleware with a sync hook, which now stops startup.

## Alternatives considered

- **Keep the phase's deny status (0010).** A crash and a policy decision look the same to the client.
  The log and the failures counter tell them apart, but only for whoever reads them.
- **A new outcome kind (`error:<name>`) for a fail-closed failure.** Clearer in the access log, but it
  changes `on_done`'s outcomes and the histograms' `outcome` label as well; the decision was the status
  and the body. Left for a later record if operators need it.
- **Check sync hooks in `load_middleware` only.** Middleware passed to `ProxyServer(middleware=...)` or
  `Pipeline` directly (tests, embedding) would skip the check; checking in `register` covers every path.

## Consequences

- A gate's crash is a `502` that names it; a `403` from a middleware is always a decision.
- A sync hook is found once, at startup or on reload, not on every flow.
- Rollback: revert the PR; no setting or metric changes.
- Wiki: Writing-Middleware (the statuses, async hooks, timeouts and cancellation), Troubleshooting (the
  `502 middleware <name> failed` body, the startup error, timeouts), Security (fail-closed gates answer
  `502`), Architecture-Internals (the failure rule), Testing-Guide (`test_failure_policy.py`).

## Open questions

None. 0010's remaining open question (`on_error` and `timeout` from configuration) stays open there.
