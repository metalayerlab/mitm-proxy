import asyncio
import logging

import pytest

from mitm_proxy.errors import StartupError
from mitm_proxy.http.body import BufferedBodyStream
from mitm_proxy.http.flow import ClientConn, Flow, FlowType, Request, Response, ServerConn
from mitm_proxy.middleware.log import FlowLogger
from mitm_proxy.pipeline import Action, Middleware, Pipeline, is_mutation_capable, load_middleware


def make_flow() -> Flow:
    return Flow(
        type=FlowType.HTTP,
        protocol="http/1.1",
        client_conn=ClientConn(("192.0.2.1", 50000)),
        request=Request("GET", "https", "example.org", "/"),
    )


class Recorder(Middleware):
    def __init__(self, calls: list[str], tag: str, action: Action = Action.CONTINUE) -> None:
        self.calls, self.tag, self.action = calls, tag, action

    async def on_request(self, flow: Flow) -> Action:
        self.calls.append(self.tag)
        return self.action


class Upper(Middleware):
    async def on_response_chunk(self, flow: Flow, chunk: bytes, is_last: bool) -> bytes | None:
        return chunk.upper()


class Suffix(Middleware):
    async def on_response_chunk(self, flow: Flow, chunk: bytes, is_last: bool) -> bytes | None:
        return chunk + b"!"


async def test_runs_in_registration_order_until_deny() -> None:
    calls: list[str] = []
    pipe = Pipeline(
        [
            ("a", Recorder(calls, "a")),
            ("b", Recorder(calls, "b", Action.DENY)),
            ("c", Recorder(calls, "c")),
        ]
    )
    outcome = await pipe.start(make_flow()).run_request()
    assert outcome.action is Action.DENY and outcome.by == "b"
    assert calls == ["a", "b"]
    assert pipe.names == ["a", "b", "c"]


async def test_chunk_hooks_chain_their_output() -> None:
    run = Pipeline([("upper", Upper()), ("suffix", Suffix())]).start(make_flow())
    assert run.mutation_capable
    assert await run.run_response_chunk(b"abc", True) == b"ABC!"
    assert await run.run_request_chunk(b"abc", True) == b"abc"


async def test_applies_to_filters_and_its_failure_excludes() -> None:
    class Never(Upper):
        def applies_to(self, flow: Flow) -> bool:
            return False

    class Raises(Upper):
        def applies_to(self, flow: Flow) -> bool:
            raise ValueError("boom")

    run = Pipeline([("never", Never()), ("raises", Raises())]).start(make_flow())
    assert not run.applies and not run.mutation_capable
    assert run.failed == {"raises"}


async def test_a_failing_hook_is_logged_and_skipped_for_the_flow(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Boom(Middleware):
        def __init__(self) -> None:
            self.calls = 0

        async def on_response_chunk(self, flow: Flow, chunk: bytes, is_last: bool) -> bytes | None:
            self.calls += 1
            raise RuntimeError("bad middleware")

    boom = Boom()
    failures: list[str] = []
    flow = make_flow()
    run = Pipeline([("boom", boom), ("upper", Upper())]).start(flow)
    run.on_failure = failures.append
    assert await run.run_response_chunk(b"a", False) == b"A"
    assert await run.run_response_chunk(b"b", True) == b"B"
    assert boom.calls == 1
    assert failures == ["boom"]
    assert "'boom' failed in on_response_chunk" in caplog.text and flow.id in caplog.text


async def test_a_hanging_hook_times_out(caplog: pytest.LogCaptureFixture) -> None:
    class Hang(Middleware):
        async def on_request(self, flow: Flow) -> Action:
            await asyncio.sleep(10)
            return Action.DENY

    run = Pipeline([("hang", Hang())], hook_timeout=0.05).start(make_flow())
    assert (await run.run_request()).action is Action.CONTINUE
    assert run.failed == {"hang"}
    assert "timed out" in caplog.text


async def test_wrong_return_types_count_as_failures(caplog: pytest.LogCaptureFixture) -> None:
    class Wrong(Middleware):
        async def on_request(self, flow: Flow) -> Action:
            return "deny"  # type: ignore[return-value]  # the mistake under test

        async def on_response_chunk(self, flow: Flow, chunk: bytes, is_last: bool) -> bytes | None:
            return "text"  # type: ignore[return-value]  # the mistake under test

    run = Pipeline([("wrong", Wrong())]).start(make_flow())
    assert (await run.run_request()).action is Action.CONTINUE
    assert "not an Action" in caplog.text
    run2 = Pipeline([("wrong", Wrong())]).start(make_flow())
    assert await run2.run_response_chunk(b"x", True) == b"x"
    assert "not bytes or None" in caplog.text


async def test_skipped_phases_run_nothing() -> None:
    calls: list[str] = []
    run = Pipeline([("a", Recorder(calls, "a", Action.DENY)), ("u", Upper())]).start(make_flow())
    run.request_skipped = run.response_skipped = True
    assert (await run.run_request()).action is Action.CONTINUE
    assert await run.run_request_chunk(b"x", True) == b"x"
    assert (await run.run_response()).action is Action.CONTINUE
    assert await run.run_response_chunk(b"x", True) == b"x"
    assert calls == []


async def test_hooks_are_awaited_never_backgrounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backpressure rule: the runner must not schedule hooks as tasks."""
    done: list[bool] = []

    class Slow(Middleware):
        async def on_response_chunk(self, flow: Flow, chunk: bytes, is_last: bool) -> bytes | None:
            await asyncio.sleep(0.01)
            done.append(True)
            return None

    def no_tasks(*args: object, **kwargs: object) -> None:
        raise AssertionError("the pipeline runner created a task")

    run = Pipeline([("slow", Slow())]).start(make_flow())
    monkeypatch.setattr(asyncio, "create_task", no_tasks)
    await run.run_response_chunk(b"x", True)
    assert done == [True]  # finished before run_response_chunk returned


def test_registration_rules() -> None:
    pipe = Pipeline()
    pipe.register("a", Middleware())
    with pytest.raises(ValueError, match="twice"):
        pipe.register("a", Middleware())
    with pytest.raises(ValueError, match="name"):
        pipe.register("", Middleware())
    with pytest.raises(TypeError):
        pipe.register("b", object())  # type: ignore[arg-type]  # the mistake under test
    assert not is_mutation_capable(Middleware())
    assert is_mutation_capable(Upper())


def test_load_middleware() -> None:
    mod = "tests.unit.sample_middleware"
    available = {
        "cls": f"{mod}:Sample",
        "inst": f"{mod}:instance",
        "log": "mitm_proxy.middleware.log:FlowLogger",
    }
    loaded = load_middleware(available, ["cls", "inst", "log"])
    assert [n for n, _ in loaded] == ["cls", "inst", "log"]
    assert isinstance(loaded[2][1], FlowLogger)


@pytest.mark.parametrize(
    ("ref", "message"),
    [
        ("tests.unit.sample_middleware:not_middleware", "is not a Middleware"),
        ("tests.unit.sample_middleware:Missing", "cannot load"),
        ("no_such_module_xyz:Thing", "cannot load"),
        ("tests.unit.sample_middleware:Broken", "failed"),
    ],
)
def test_load_middleware_errors(ref: str, message: str) -> None:
    with pytest.raises(StartupError, match=message):
        load_middleware({"x": ref}, ["x"])


async def test_flow_logger_logs_one_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="mitm_proxy.flows")
    flow = make_flow()
    flow.server_conn = ServerConn("example.org", 443, protocol="h2")
    logger = FlowLogger()
    assert await logger.on_request(flow) is Action.CONTINUE
    flow.response = Response(200, body=BufferedBodyStream(b"hello"))
    t = flow.timings
    t.upstream_requested, t.upstream_acquired, t.connection_reused = 10.0, 10.0015, True
    t.request_sent, t.response_headers = 10.002, 10.0312
    t.middleware["a"] = 0.0004
    assert await logger.on_response(flow) is Action.CONTINUE
    line = caplog.records[-1].getMessage()
    assert f"{flow.id} http/1.1>h2 GET https://example.org/ 200 req=0 resp=5 total=" in line
    assert line.endswith("ms connect=1.5ms(reused) ttfb=29.2ms mw=0.4ms")


async def test_flow_logger_prints_a_dash_for_a_stage_that_did_not_happen(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="mitm_proxy.flows")
    flow = make_flow()
    flow.response = Response.make(200, b"cached")
    await FlowLogger().on_response(flow)
    assert caplog.records[-1].getMessage().endswith(" connect=- ttfb=- mw=0.0ms")


async def test_hook_time_is_summed_per_middleware_and_kept_on_failure() -> None:
    class SlowThenBroken(Middleware):
        async def on_request(self, flow: Flow) -> Action:
            await asyncio.sleep(0.02)
            return Action.CONTINUE

        async def on_response(self, flow: Flow) -> Action:
            await asyncio.sleep(0.02)
            raise RuntimeError("boom")

    flow = make_flow()
    run = Pipeline([("slow", SlowThenBroken()), ("quick", Middleware())]).start(flow)
    await run.run_request()
    await run.run_response()
    spent = flow.timings.middleware
    assert spent["slow"] >= 0.04
    assert spent["quick"] < 0.02
    assert flow.timings.middleware_total == pytest.approx(spent["slow"] + spent["quick"])


def test_timings_derive_durations_and_none_for_missing_stages() -> None:
    t = make_flow().timings
    assert (t.connect, t.ttfb, t.total, t.middleware_total) == (None, None, None, 0.0)
    t.started, t.upstream_requested, t.upstream_acquired = 0.5, 1.0, 1.25
    t.request_sent, t.response_headers, t.complete = 1.5, 2.0, 3.0
    assert (t.connect, t.ttfb, t.total) == (0.25, 0.5, 2.5)
