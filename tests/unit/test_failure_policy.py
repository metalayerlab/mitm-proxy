"""The pipeline on interpose's core: mitm-proxy's policy and per-middleware on_error and timeout
(design 0010)."""

import asyncio
from pathlib import Path
from typing import ClassVar

import pytest
from interpose.pipeline import HookFailed, OnError

from mitm_proxy.errors import StartupError
from mitm_proxy.http.flow import Flow, Outcome
from mitm_proxy.pipeline import (
    NO_LIMIT,
    Action,
    Middleware,
    Pipeline,
    load_middleware,
    policy,
)
from mitm_proxy.pipeline import Outcome as RunOutcome
from tests.harness import Origin, bench, client_for, send
from tests.unit.test_on_done import Done, _told
from tests.unit.test_pipeline import Upper, make_flow


def test_the_policy_is_todays_behaviour() -> None:
    p = policy(2.5)
    assert (p.on_error, p.after_order, p.after_local, p.observe_failed) == (
        "skip",
        "chain",
        False,
        False,
    )
    assert p.timeout == 2.5 and p.answers == () and p.skip_inherited is None
    assert policy().timeout == 5.0
    assert Middleware.on_error is None and Middleware.timeout is None


class Gate(Middleware):
    """A blocklist that fails closed: a crash must not let traffic through."""

    on_error: ClassVar[OnError | None] = "fail"

    def __init__(self, where: str) -> None:
        self.where = where

    def applies_to(self, flow: Flow) -> bool:
        if self.where == "applies_to":
            raise RuntimeError("boom")
        return True

    async def on_request(self, flow: Flow) -> Action:
        if self.where == "on_request":
            raise RuntimeError("boom")
        return Action.CONTINUE

    async def on_request_chunk(self, flow: Flow, chunk: bytes, is_last: bool) -> bytes | None:
        if self.where == "on_request_chunk":
            raise RuntimeError("boom")
        return None

    async def on_response(self, flow: Flow) -> Action:
        if self.where == "on_response":
            raise RuntimeError("boom")
        return Action.CONTINUE

    async def on_response_chunk(self, flow: Flow, chunk: bytes, is_last: bool) -> bytes | None:
        if self.where == "on_response_chunk":
            raise RuntimeError("boom")
        return None

    async def on_done(self, flow: Flow) -> None:
        if self.where == "on_done":
            raise RuntimeError("boom")


@pytest.mark.parametrize(
    ("where", "told"),
    [
        # A fail-closed failure is a 502 naming the middleware, in both phases (0011).
        ("applies_to", False),  # nothing was selected, so no on_done
        ("on_request", True),
        ("on_request_chunk", True),
        ("on_response", True),
        ("on_response_chunk", True),
    ],
)
@pytest.mark.parametrize("protocol", ["http/1.1", "h2"])
async def test_a_crashing_gate_blocks_the_flow_with_a_502(
    where: str, told: bool, protocol: str
) -> None:
    origin = Origin(echo=True)
    b = bench(origin)
    done = Done()
    ctx = b.context(protocol, [("gate", Gate(where)), ("done", done)])
    client = await client_for(b.proxy(ctx), protocol)
    reply = await send(client, "POST", body=b"abc")
    assert reply.status == 502
    # The name only: never the exception's text ("boom") or a value from the flow.
    assert reply.body == b"middleware gate failed\n"
    assert b.metrics.middleware_failures.values == {("gate",): 1.0}
    if told:
        await _told(done)
        assert done.seen[0][1] == Outcome("denied", "gate", 502)
    else:
        assert done.seen == []
    await b.close()


class Denies(Middleware):
    """A gate that decides to deny: unchanged by 0011, even when it fails closed."""

    on_error: ClassVar[OnError | None] = "fail"

    def __init__(self, phase: str) -> None:
        self.phase = phase

    async def on_request(self, flow: Flow) -> Action:
        return Action.DENY if self.phase == "request" else Action.CONTINUE

    async def on_response(self, flow: Flow) -> Action:
        return Action.DENY if self.phase == "response" else Action.CONTINUE


@pytest.mark.parametrize(("phase", "status"), [("request", 403), ("response", 502)])
async def test_a_deliberate_deny_is_unchanged(phase: str, status: int) -> None:
    b = bench()
    client = await client_for(b.proxy(b.context("http/1.1", [("gate", Denies(phase))])), "http/1.1")
    reply = await send(client)
    assert reply.status == status and reply.body == b"denied by middleware gate\n"
    assert b.metrics.middleware_failures.values == {}
    await b.close()


async def test_a_fail_closed_failure_is_marked_failed_and_a_deny_is_not() -> None:
    run = Pipeline([("gate", Gate("on_request"))]).start(make_flow())
    assert await run.run_request() == RunOutcome(Action.DENY, "gate", failed=True)
    run = Pipeline([("gate", Gate("applies_to"))]).start(make_flow())
    assert await run.run_request() == RunOutcome(Action.DENY, "gate", failed=True)
    run = Pipeline([("gate", Denies("request"))]).start(make_flow())
    assert await run.run_request() == RunOutcome(Action.DENY, "gate")


async def test_a_gate_whose_on_done_fails_is_still_fail_open_there() -> None:
    b = bench()
    done = Done()
    ctx = b.context("http/1.1", [("gate", Gate("on_done")), ("done", done)])
    client = await client_for(b.proxy(ctx), "http/1.1")
    assert (await send(client)).status == 200
    await _told(done)
    assert b.metrics.middleware_failures.values == {("gate",): 1.0}
    await b.close()


async def test_a_failing_fail_open_middleware_still_lets_the_flow_through() -> None:
    class Crashes(Middleware):
        async def on_request(self, flow: Flow) -> Action:
            raise RuntimeError("boom")

    b = bench()
    client = await client_for(b.proxy(b.context("h2", [("crashes", Crashes())])), "h2")
    assert (await send(client)).status == 200
    await b.close()


async def test_a_fail_closed_applies_to_failure_denies_even_an_oversize_body() -> None:
    b = bench(Origin(echo=True))
    ctx = b.context("h2", [("gate", Gate("applies_to"))], max_buffer=2)
    client = await client_for(b.proxy(ctx), "h2")
    assert (await send(client, "POST", body=b"more than two bytes")).status == 502
    await b.close()


def test_a_fail_closed_applies_to_failure_selects_nothing_and_denies() -> None:
    calls: list[str] = []

    class Later(Middleware):
        def applies_to(self, flow: Flow) -> bool:
            calls.append("later")
            return True

    run = Pipeline([("gate", Gate("applies_to")), ("later", Later())]).start(make_flow())
    assert run.applies and run.entries == [] and run.denied_by == "gate"
    assert run.failed == {"gate"} and calls == []


async def test_chunk_hooks_raise_hook_failed_for_the_exchange() -> None:
    run = Pipeline([("gate", Gate("on_response_chunk"))]).start(make_flow())
    with pytest.raises(HookFailed) as e:
        await run.run_response_chunk(b"x", True)
    assert e.value.failure.middleware == "gate"
    assert run.failed == {"gate"}


async def test_a_middleware_timeout_attribute_replaces_the_hook_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Quick(Middleware):
        timeout: ClassVar[float | None] = 0.02

        async def on_request(self, flow: Flow) -> Action:
            await asyncio.sleep(1)
            return Action.DENY

    class Patient(Middleware):
        timeout: ClassVar[float | None] = NO_LIMIT

        async def on_request(self, flow: Flow) -> Action:
            await asyncio.sleep(0.05)
            return Action.STOP

    run = Pipeline([("quick", Quick())], hook_timeout=5).start(make_flow())
    assert (await run.run_request()).action is Action.CONTINUE
    assert "'quick' timed out in on_request" in caplog.text
    run = Pipeline([("patient", Patient())], hook_timeout=0.01).start(make_flow())
    assert (await run.run_request()).action is Action.STOP


async def test_every_chunk_middleware_gets_bytes_from_the_one_before() -> None:
    seen: list[type] = []

    class Bytearray(Middleware):
        async def on_response_chunk(self, flow: Flow, chunk: bytes, is_last: bool) -> bytes | None:
            return bytearray(chunk + b"!")  # type: ignore[return-value]  # accepted, made bytes

    class Looks(Middleware):
        async def on_response_chunk(self, flow: Flow, chunk: bytes, is_last: bool) -> bytes | None:
            seen.append(type(chunk))
            return None

    run = Pipeline([("ba", Bytearray()), ("looks", Looks())]).start(make_flow())
    result = await run.run_response_chunk(b"a", True)
    assert result == b"a!" and type(result) is bytes and seen == [bytes]


async def test_a_number_from_a_chunk_hook_is_a_failure_not_zero_bytes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Number(Middleware):
        async def on_request_chunk(self, flow: Flow, chunk: bytes, is_last: bool) -> bytes | None:
            return 3  # type: ignore[return-value]  # the mistake under test

    run = Pipeline([("number", Number()), ("upper", Upper())]).start(make_flow())
    assert await run.run_request_chunk(b"x", True) == b"x"
    assert run.failed == {"number"}
    assert "'number' returned 'int' from on_request_chunk, not bytes or None" in caplog.text


async def test_an_invalid_phase_result_is_logged_by_type_never_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Wrong(Middleware):
        async def on_request(self, flow: Flow) -> Action:
            return "secret-value"  # type: ignore[return-value]  # the mistake under test

    run = Pipeline([("wrong", Wrong())]).start(make_flow())
    assert (await run.run_request()).action is Action.CONTINUE
    assert "'wrong' returned 'str' from on_request, not an Action" in caplog.text
    assert "secret-value" not in caplog.text


async def test_a_timeout_error_the_hook_raises_is_a_failure_not_a_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class OwnTimeout(Middleware):
        async def on_request(self, flow: Flow) -> Action:
            raise TimeoutError("its own upstream call")

    failures: list[str] = []
    run = Pipeline([("own", OwnTimeout())]).start(make_flow(), failures.append)
    assert (await run.run_request()).action is Action.CONTINUE
    assert failures == ["own"] and run.failed == {"own"}
    assert "'own' failed in on_request" in caplog.text and "timed out" not in caplog.text


async def test_on_response_reaches_what_ran_on_request_and_has_not_failed() -> None:
    calls: list[str] = []

    class Tracks(Middleware):
        def __init__(self, tag: str, fail: bool = False) -> None:
            self.tag, self.fail = tag, fail

        async def on_request(self, flow: Flow) -> Action:
            if self.fail:
                raise RuntimeError("boom")
            return Action.CONTINUE

        async def on_response(self, flow: Flow) -> Action:
            calls.append(self.tag)
            return Action.CONTINUE

    run = Pipeline([("a", Tracks("a")), ("b", Tracks("b", fail=True)), ("c", Tracks("c"))]).start(
        make_flow()
    )
    await run.run_request()
    assert (await run.run_response()).action is Action.CONTINUE
    assert calls == ["a", "c"]


async def test_flow_metadata_is_the_runs_state() -> None:
    class Notes(Middleware):
        async def on_request(self, flow: Flow) -> Action:
            flow.metadata["notes.seen"] = True
            return Action.CONTINUE

    flow = make_flow()
    run = Pipeline([("notes", Notes())]).start(flow)
    await run.run_request()
    assert flow.metadata == {"notes.seen": True}


@pytest.mark.parametrize(
    ("attribute", "value"),
    [("on_error", "sometimes"), ("timeout", 0), ("timeout", "30s")],
)
def test_a_wrong_policy_attribute_is_refused_at_startup(attribute: str, value: object) -> None:
    middleware = Middleware()
    setattr(middleware, attribute, value)
    with pytest.raises(ValueError, match=r"'odd'.*its on_error or timeout attribute"):
        Pipeline([("odd", middleware)])


def test_load_middleware_refuses_a_wrong_policy_attribute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit import sample_middleware

    monkeypatch.setattr(sample_middleware.Sample, "on_error", "never", raising=False)
    with pytest.raises(StartupError, match=r"middleware 'x'.*on_error must be"):
        load_middleware({"x": "tests.unit.sample_middleware:Sample"}, ["x"])


class SyncHook(Middleware):
    def on_request(self, flow: Flow) -> Action:  # type: ignore[override]  # the mistake under test
        return Action.CONTINUE


class SyncDone(Middleware):
    def on_done(self, flow: Flow) -> None:  # type: ignore[override]  # the mistake under test
        return None


@pytest.mark.parametrize(
    ("middleware", "hook"), [(SyncHook(), "on_request"), (SyncDone(), "on_done")]
)
def test_a_sync_hook_is_refused_when_registered(middleware: Middleware, hook: str) -> None:
    with pytest.raises(TypeError, match=rf"middleware 'odd': {hook} must be an async function"):
        Pipeline([("odd", middleware)])


def test_applies_to_may_stay_a_plain_function() -> None:
    assert Pipeline([("gate", Gate("nowhere"))]).names == ["gate"]


def test_load_middleware_refuses_a_sync_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit import sample_middleware

    def on_response(self: Middleware, flow: Flow) -> Action:
        return Action.CONTINUE

    monkeypatch.setattr(sample_middleware.Sample, "on_response", on_response)
    with pytest.raises(
        StartupError, match=r"middleware 'x': on_response must be an async function"
    ):
        load_middleware({"x": "tests.unit.sample_middleware:Sample"}, ["x"])


def test_a_sync_hook_is_a_startup_error(tmp_path: Path) -> None:
    from mitm_proxy.server import ProxyServer
    from tests.unit.test_server import make_config

    config = make_config(
        tmp_path,
        MITM_PROXY_MIDDLEWARE_AVAILABLE="sync=tests.unit.sample_middleware:SyncHook",
        MITM_PROXY_MIDDLEWARE_CHAIN="sync",
    )
    with pytest.raises(StartupError, match=r"middleware 'sync': on_request must be an async"):
        ProxyServer(config, connector=Origin().connector)


async def test_a_reload_with_a_sync_hook_is_refused_and_keeps_the_old_pipeline(
    tmp_path: Path,
) -> None:
    from mitm_proxy.server import ProxyServer
    from tests.unit.test_server import make_config

    available = (
        "sample=tests.unit.sample_middleware:Sample,sync=tests.unit.sample_middleware:SyncHook"
    )
    proxy = ProxyServer(
        make_config(
            tmp_path,
            MITM_PROXY_MIDDLEWARE_AVAILABLE=available,
            MITM_PROXY_MIDDLEWARE_CHAIN="sample",
        ),
        connector=Origin().connector,
    )
    before = proxy.snapshot
    refused = make_config(
        tmp_path, MITM_PROXY_MIDDLEWARE_AVAILABLE=available, MITM_PROXY_MIDDLEWARE_CHAIN="sync"
    )
    with pytest.raises(StartupError, match=r"middleware 'sync': on_request must be an async"):
        proxy.apply(refused)
    # Like any other config error on apply: the running snapshot stays, and isn't counted.
    assert proxy.snapshot is before and proxy.snapshot.pipeline.names == ["sample"]
    assert proxy.metrics.config_applies.values == {("applied",): 1.0}
    await proxy.stop()
