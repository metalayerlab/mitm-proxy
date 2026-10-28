# Testing Guide

## Running the tests

```sh
uv run pytest                                   # unit tests, with an 80% coverage floor
uv run pytest tests/unit/test_config.py -k env  # a subset
RUN_INTEGRATION=1 uv run pytest -m integration  # integration tests (real external systems)
```

CI runs the unit tests on every supported Python version, together with ruff, mypy (strict), the wiki checks and the manifest checks. All of them block a merge.

## Test layout

| Path | What goes there |
|---|---|
| `tests/unit/` | Fast and offline: no network, no real external systems. Most tests. |
| `tests/integration/` | Real dependencies, marked `@pytest.mark.integration`; skipped unless `RUN_INTEGRATION=1` |
| `tests/conftest.py` | Shared fixtures: `clean_env` (no `MITM_PROXY_*` leakage), `write_config` |

| `tests/conftest.py` | Also runs `async def` tests on a fresh event loop, failing any that run over 10 s |
| `tests/helpers.py` | `memory_pair()` (an in-memory `ByteStream` pair), `make_ca()`, `client_hello_bytes()`, `handshake_in_memory()`, `trusting_client_context()` |
| `tests/harness.py` | The proxy test bench: `Origin` (a fake origin speaking HTTP/1.1 or HTTP/2), `bench()`, `client_for()`, `send()` |
| `tests/unit/sample_middleware.py` | Middleware for import-by-name tests |
| `tests/integration/test_proxy_e2e.py` | The real listener over loopback, a verified TLS origin, and shutdown on SIGTERM |

## Writing tests

- Every behaviour change needs a test that fails without it. A bug fix starts with the failing test.
- Assert outcomes (outputs, side effects, errors), not internal calls.
- Use placeholders for anything that looks real: `example.org`, `192.0.2.0/24`, fake tokens.
- Don't lower the coverage floor or skip a test to make CI pass.

**No sockets in unit tests.** Everything runs over `memory_pair()` streams on one loop. TLS works in memory too: `TLSStream` over a memory pair, with `make_ca()` and `trusting_client_context()`.

**Faking the origin.** `Origin` is a fake origin server. Its `connector` plugs into the pool, so the proxy "connects" to it in memory. Set `protocol` (`"h2"` or `"http/1.1"`, as if ALPN chose it), `status`, `body`, `trailers`, `echo=True` to answer with the request body, `websocket=True` to accept upgrades, or `fail_connect=` an `UpstreamError` to simulate refusal, timeouts or certificate failures. What it received is in `origin.seen`.

```python
async def test_example() -> None:
    b = bench(Origin(protocol="h2", echo=True))
    client = await client_for(b.proxy(b.context("http/1.1", [("upper", Upper())])), "http/1.1")
    reply = await send(client, "POST", body=b"abc")
    assert reply.body == b"ABC"
    await b.close()
```

**Every protocol pairing.** Behaviour that touches framing or headers is parametrized over the four client/origin pairings (`PAIRS` in `test_exchange.py`).

**Time.** Use short timeouts (tens of milliseconds) rather than patching the clock; `hook_timeout`, `peek_timeout_ms` and `idle_timeout` are all settable.

**Integration tests** bind `127.0.0.1` on free ports and never leave the machine.
