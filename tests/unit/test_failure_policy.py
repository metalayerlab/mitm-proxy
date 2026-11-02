"""The pipeline on interpose's core: mitm-proxy's policy and per-middleware on_error and timeout
(design 0010)."""

import asyncio
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
    ("where", "status", "told"),
    [
        # A request-phase failure is answered like the request-phase DENY: 403.
        ("applies_to", 403, False),  # nothing was selected, so no on_done
        ("on_request", 403, True),
        ("on_request_chunk", 403, True),
        # A response-phase failure, like the response-phase DENY: 502.
        ("on_response", 502, True),
        ("on_response_chunk", 502, True),
    ],
)
async def test_a_crashing_gate_blocks_the_flow(where: str, status: int, told: bool) -> None:
    origin = Origin(echo=True)
    b = bench(origin)
    done = Done()
    ctx = b.context("http/1.1", [("gate", Gate(where)), ("done", done)])
    client = await client_for(b.proxy(ctx), "http/1.1")
    reply = await send(client, "POST", body=b"abc")
    assert reply.status == status
    assert reply.body == b"denied by middleware gate\n"
    assert b.metrics.middleware_failures.values == {("gate",): 1.0}
    if told:
        await _told(done)
        assert done.seen[0][1] == Outcome("denied", "gate", status)
    else:
        assert done.seen == []
    await b.close()


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
    assert (await send(client, "POST", body=b"more than two bytes")).status == 403
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
