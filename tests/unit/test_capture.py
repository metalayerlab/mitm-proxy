"""The built-in capture middleware (design 0012, section 3; step (c), buffered)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime
import gzip
import json
import logging
import os
import socket
import stat
import zlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from mitm_proxy.config import ConfigError, Section, load_config
from mitm_proxy.errors import StartupError, UpstreamConnectError
from mitm_proxy.http.flow import ClientConn, Flow, FlowType, Request, Response
from mitm_proxy.http.h1 import H1Server, H1Upstream
from mitm_proxy.http.headers import Headers
from mitm_proxy.http.messages import IncomingRequest, Responder
from mitm_proxy.middleware import BUILTIN
from mitm_proxy.middleware.capture import (
    DEFAULT_REDACT_HEADERS,
    Capture,
    JsonPath,
    _decompress,
)
from mitm_proxy.pipeline import Action, Middleware, load_middleware
from mitm_proxy.server import ProxyServer
from mitm_proxy.streams import PlainStream
from tests.harness import Origin, bench, client_for, send

FAKE_KEY = "sk-test-not-a-real-key"  # a made-up credential: it must never reach the file


def options(tmp_path: Path, **extra: Any) -> Section:
    """capture's options as middleware.config gives them: a read-only Section."""
    base: dict[str, Any] = {"dir": str(tmp_path / "cap"), "rules": [{"name": "all"}]}
    base.update(extra)
    return Section(base)


def records(directory: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.jsonl")):
        out.extend(json.loads(line) for line in path.read_text("utf-8").splitlines())
    return out


class Counted:
    """Collects capture's (rule, result) calls, and waits for them."""

    def __init__(self, capture: Capture) -> None:
        self.calls: list[tuple[str, str]] = []
        capture.on_record = lambda rule, result: self.calls.append((rule, result))

    async def wait(self, count: int = 1) -> None:
        while len(self.calls) < count:
            await asyncio.sleep(0.001)


async def run_one(
    capture: Capture,
    origin: Origin | None = None,
    *,
    protocol: str = "http/1.1",
    before: list[tuple[str, Middleware]] | None = None,
    max_buffer: int = 1 << 20,
    **send_args: Any,
) -> int:
    """One flow through the in-memory bench with capture in the chain; the client's status."""
    b = bench(origin)
    counted = Counted(capture)
    ctx = b.context(protocol, [*(before or []), ("capture", capture)], max_buffer=max_buffer)
    client = await client_for(b.proxy(ctx), protocol)
    reply = await send(client, **send_args)
    await counted.wait()
    await b.close()
    return reply.status


def flow_for(
    method: str = "GET",
    path: str = "/",
    authority: str = "example.org",
    direction: str = "egress",
) -> Flow:
    return Flow(
        type=FlowType.HTTP,
        protocol="http/1.1",
        client_conn=ClientConn(("192.0.2.1", 1)),
        request=Request(method, "https", authority, path),
        direction=direction,
    )


# 1. A built-in name: always available, never shadowed, run only from the chain.


def test_capture_is_a_built_in_name() -> None:
    assert BUILTIN["capture"] == "mitm_proxy.middleware.capture:Capture"


def _config(tmp_path: Path, yaml: str) -> Any:
    path = tmp_path / "config.yaml"
    path.write_text(yaml, encoding="utf-8")
    return load_config(path, environ={"MITM_PROXY_STATE_DIR": str(tmp_path)})


def test_the_chain_and_middleware_config_may_name_capture_without_an_available_entry(
    tmp_path: Path,
) -> None:
    cfg = _config(
        tmp_path,
        "middleware:\n  chain: [capture]\n  config:\n"
        f"    capture: {{dir: {tmp_path / 'cap'}, rules: [{{name: all}}]}}\n",
    )
    assert cfg.middleware_chain == ("capture",)
    assert dict(cfg.middleware_available) == {}  # the registry is the user's; built-ins aside
    loaded = load_middleware(cfg.middleware_available, cfg.middleware_chain, cfg.middleware_config)
    assert [name for name, _ in loaded] == ["capture"]
    assert isinstance(loaded[0][1], Capture)


def test_capture_runs_only_when_the_chain_names_it(tmp_path: Path) -> None:
    cfg = _config(tmp_path, "proxy:\n  port: 3128\n")
    assert load_middleware(cfg.middleware_available, cfg.middleware_chain) == []


def test_an_available_entry_named_capture_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"'capture' is a built-in middleware"):
        _config(tmp_path, "middleware:\n  available: ['capture=pkg.mod:Mine']\n")


def test_an_unknown_name_still_lists_the_built_ins(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"'nope' is not in middleware.available .*capture"):
        _config(tmp_path, "middleware:\n  chain: [nope]\n")


def test_capture_in_the_chain_without_options_is_a_startup_error(tmp_path: Path) -> None:
    with pytest.raises(StartupError, match=r"middleware 'capture'.*needs options"):
        load_middleware({}, ["capture"])


# 2. Options: exactly record 0012's table, with an unknown key at any level refused.


def test_defaults(tmp_path: Path) -> None:
    capture = Capture(options(tmp_path))
    assert capture.dir == tmp_path / "cap"
    assert capture.max_body_bytes == 1048576
    assert capture.redact_headers == frozenset(DEFAULT_REDACT_HEADERS)
    assert capture.json_paths == () and capture.query_params == frozenset()
    assert capture.marker == "[REDACTED]"
    rule = capture.rules[0]
    assert (rule.direction, rule.host, rule.path, rule.methods) == ("any", None, None, None)


def test_a_given_header_list_replaces_the_default(tmp_path: Path) -> None:
    capture = Capture(options(tmp_path, redact={"headers": ["X-Token"]}))
    assert capture.redact_headers == frozenset({"x-token"})


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"redcat": {}}, r"middleware.config.capture: unknown key 'redcat'"),
        ({"redact": {"header": ["a"]}}, r"redact: unknown key 'header'"),
        ({"rules": [{"name": "a", "hosts": "x"}]}, r"rules\[0\]: unknown key 'hosts'"),
    ],
)
def test_an_unknown_key_at_any_level_is_a_startup_error(
    tmp_path: Path, extra: dict[str, Any], message: str
) -> None:
    with pytest.raises(StartupError, match=message):
        Capture(options(tmp_path, **extra))


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"dir": ""}, "dir is required"),
        ({"dir": 3}, "dir: expected a string"),
        ({"max_body_bytes": -1}, "max_body_bytes"),
        ({"max_body_bytes": True}, "max_body_bytes"),
        ({"rules": []}, "rules: expected a list of one or more"),
        ({"rules": [{}]}, r"rules\[0\]: name is required"),
        ({"rules": [{"name": "Upper"}]}, "lower-case letters"),
        ({"rules": [{"name": "a"}, {"name": "a"}]}, "used twice"),
        ({"rules": [{"name": "a", "direction": "up"}]}, "direction"),
        ({"rules": [{"name": "a", "host": "*"}]}, r"rules\[0\].host"),
        ({"rules": [{"name": "a", "path": "v1"}]}, "must start with /"),
        ({"rules": [{"name": "a", "path": "/a?b"}]}, "without its query"),
        ({"rules": [{"name": "a", "methods": []}]}, "at least one method"),
        ({"rules": "a"}, "rules: expected a list"),
        ({"redact": {"json_paths": ["api_key"]}}, r"json_paths\[0\]: 'api_key': a path starts"),
        ({"redact": {"json_paths": ["$.a b"]}}, r"json_paths\[0\]"),
        ({"redact": {"marker": 1}}, "redact.marker: expected a string"),
        ({"redact": {"query_params": "api_key"}}, "query_params: expected a list"),
        ({"redact": "all"}, "redact: expected a mapping"),
    ],
)
def test_invalid_options(tmp_path: Path, extra: dict[str, Any], message: str) -> None:
    with pytest.raises(StartupError, match=message):
        Capture(options(tmp_path, **extra))


def test_dir_and_rules_are_required(tmp_path: Path) -> None:
    with pytest.raises(StartupError, match="dir is required"):
        Capture(Section({"rules": [{"name": "a"}]}))
    with pytest.raises(StartupError, match="rules is required"):
        Capture(Section({"dir": str(tmp_path)}))


# 3. Selection: direction, host, path, methods; the first matching rule names the record.


def test_selection_by_direction_host_path_and_method(tmp_path: Path) -> None:
    capture = Capture(
        options(
            tmp_path,
            rules=[
                {
                    "name": "api",
                    "direction": "inbound",
                    "host": "api.internal",
                    "path": "/v1/**",
                    "methods": ["post"],
                },
                {"name": "llm", "direction": "egress", "host": "*.llm.example", "path": "/v1/*"},
            ],
        )
    )

    def rule(**kw: str) -> str | None:
        flow = flow_for(**kw)
        if not capture.applies_to(flow):
            return None
        return capture._matched[flow].name

    assert (
        rule(method="POST", authority="api.internal:8000", path="/v1/a/b?x=1", direction="inbound")
        == "api"
    )
    assert rule(method="GET", authority="api.internal", path="/v1/a", direction="inbound") is None
    assert rule(method="POST", authority="api.internal", path="/v2/a", direction="inbound") is None
    assert rule(method="POST", authority="API.internal.", path="/v1/", direction="inbound") == "api"
    assert rule(method="POST", authority="api.internal", path="/v1/a", direction="egress") is None
    # `*` stays within a segment; `**` crosses them; the query is not part of the path.
    assert rule(authority="eu.llm.example", path="/v1/chat?stream=1") == "llm"
    assert rule(authority="eu.llm.example", path="/v1/chat/more") is None
    assert rule(authority="llm.example", path="/v1/chat") is None  # *.suffix, not the suffix


def test_the_first_matching_rule_names_the_record(tmp_path: Path) -> None:
    capture = Capture(options(tmp_path, rules=[{"name": "first"}, {"name": "second"}]))
    flow = flow_for()
    assert capture.applies_to(flow) and capture._matched[flow].name == "first"


async def test_a_flow_no_rule_matches_keeps_the_fast_path(tmp_path: Path) -> None:
    capture = Capture(options(tmp_path, rules=[{"name": "only", "path": "/only"}]))
    b = bench()
    ctx = b.context("http/1.1", [("capture", capture)])
    assert ctx.pipeline is not None
    flow = flow_for(path="/other")
    assert not ctx.pipeline.start(flow).applies  # nothing applies: nothing is buffered
    await b.close()


# 4. The record.


async def test_a_record_has_record_0012s_keys(tmp_path: Path) -> None:
    capture = Capture(options(tmp_path))
    origin = Origin(headers=[("content-type", "text/plain"), ("x-a", "1"), ("x-a", "2")])
    status = await run_one(
        capture, origin, method="POST", path="/v1/answer", body=b"question", headers=[("x-q", "1")]
    )
    assert status == 200
    (record,) = records(tmp_path / "cap")
    assert list(record) == [
        "rule",
        "direction",
        "flow",
        "outcome",
        "request",
        "response",
        "timings",
        "streamed",
    ]
    assert record["rule"] == "all" and record["direction"] == "egress"
    assert len(record["flow"]) == 32 and record["outcome"] == "origin"
    assert record["streamed"] is False
    request = record["request"]
    assert request["method"] == "POST"
    assert request["url"] == "https://example.org/v1/answer"
    assert ["x-q", "1"] in request["headers"]
    assert request["body"] == "question"
    response = record["response"]
    assert response["status"] == 200 and response["body"] == "hello from origin"
    # Headers are [name, value] pairs, in order: a header can repeat.
    assert [h for h in response["headers"] if h[0] == "x-a"] == [["x-a", "1"], ["x-a", "2"]]
    timings = record["timings"]
    start = datetime.datetime.fromisoformat(timings["start"])
    end = datetime.datetime.fromisoformat(timings["end"])
    assert timings["start"].endswith("Z") and start <= end
    assert isinstance(timings["ttfb_ms"], float)


async def test_timings_are_mapped_onto_the_wall_clock(tmp_path: Path) -> None:
    wall = datetime.datetime(2026, 11, 3, 10, 15, 2, tzinfo=datetime.UTC).timestamp()
    capture = Capture(options(tmp_path), clock=lambda: wall)
    await run_one(capture)
    (record,) = records(tmp_path / "cap")
    # The flow ended just before on_done read the clock.
    assert record["timings"]["end"].startswith("2026-11-03T10:15:0")


async def test_a_binary_body_is_base64(tmp_path: Path) -> None:
    data = bytes(range(256))
    capture = Capture(options(tmp_path))
    await run_one(
        capture, Origin(headers=[("content-type", "application/octet-stream")], body=data)
    )
    response = records(tmp_path / "cap")[0]["response"]
    assert response["body_encoding"] == "base64"
    assert base64.b64decode(response["body"]) == data


async def test_a_gzip_body_is_decoded_then_redacted(tmp_path: Path) -> None:
    payload = json.dumps({"api_key": FAKE_KEY, "ok": True}).encode()
    origin = Origin(
        headers=[("content-type", "application/json"), ("content-encoding", "gzip")],
        body=gzip.compress(payload),
    )
    capture = Capture(options(tmp_path, redact={"json_paths": ["$.api_key"]}))
    await run_one(capture, origin)
    response = records(tmp_path / "cap")[0]["response"]
    assert response["content_decoded"] == "gzip"
    assert json.loads(response["body"]) == {"api_key": "[REDACTED]", "ok": True}
    assert FAKE_KEY not in (tmp_path / "cap").joinpath(os.listdir(tmp_path / "cap")[0]).read_text()


async def test_another_encoding_is_stored_as_it_came(tmp_path: Path) -> None:
    origin = Origin(headers=[("content-type", "text/plain"), ("content-encoding", "br")])
    capture = Capture(options(tmp_path))
    await run_one(capture, origin)
    response = records(tmp_path / "cap")[0]["response"]
    assert response["body_encoding"] == "base64" and "content_decoded" not in response
    assert base64.b64decode(response["body"]) == b"hello from origin"


def test_deflate_zlib_wrapped_and_raw_and_a_bomb() -> None:
    data = b"x" * 1000
    assert _decompress(zlib.compress(data), "deflate") == data
    raw = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    assert _decompress(raw.compress(data) + raw.flush(), "deflate") == data
    assert _decompress(gzip.compress(b"a") + gzip.compress(b"b"), "gzip") == b"ab"
    with pytest.raises(zlib.error):
        _decompress(b"not deflate", "deflate")
    with pytest.raises(zlib.error):
        _decompress(zlib.compress(data)[:-5], "deflate")  # cut short


def test_a_body_that_decodes_too_large_is_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    from mitm_proxy.middleware import capture as module

    monkeypatch.setattr(module, "MAX_DECODED_BYTES", 100)
    with pytest.raises(module._TooLarge):
        _decompress(gzip.compress(b"x" * 101), "gzip")
    with pytest.raises(module._TooLarge):
        _decompress(gzip.compress(b"x" * 60) + gzip.compress(b"x" * 60), "gzip")


async def test_a_body_that_fails_to_decode_is_stored_in_base64(tmp_path: Path) -> None:
    origin = Origin(headers=[("content-type", "text/plain"), ("content-encoding", "gzip")])
    capture = Capture(options(tmp_path))
    await run_one(capture, origin)
    response = records(tmp_path / "cap")[0]["response"]
    assert response["body_encoding"] == "base64" and "content_decoded" not in response


async def test_a_long_body_is_cut_after_redaction(tmp_path: Path) -> None:
    body = json.dumps({"api_key": "x" * 100}).encode()
    capture = Capture(options(tmp_path, max_body_bytes=20, redact={"json_paths": ["$.api_key"]}))
    await run_one(
        capture,
        Origin(echo=True, headers=[("content-type", "application/json")]),
        method="POST",
        body=body,
        headers=[("content-type", "application/json")],
    )
    request = records(tmp_path / "cap")[0]["request"]
    redacted = '{"api_key":"[REDACTED]"}'
    assert request["body"] == redacted[:20]
    assert request["truncated"] == len(redacted)  # the redacted length, not the original's


async def test_a_cut_in_the_middle_of_a_character_keeps_the_text(tmp_path: Path) -> None:
    capture = Capture(options(tmp_path, max_body_bytes=4))
    await run_one(capture, Origin(body="abé!".encode()))
    response = records(tmp_path / "cap")[0]["response"]
    assert response["body"] == "abé" and response["truncated"] == 5


async def test_an_oversize_body_is_omitted(tmp_path: Path) -> None:
    capture = Capture(options(tmp_path))
    await run_one(capture, method="POST", body=b"x" * 100, max_buffer=10)
    record = records(tmp_path / "cap")[0]
    assert record["request"]["body"] is None and record["request"]["omitted"] == "oversize"
    # The response skips the hooks too once the request has (0001).
    assert record["response"]["body"] is None and record["response"]["omitted"] == "oversize"


async def test_a_denied_flow_is_recorded_without_a_response(tmp_path: Path) -> None:
    class Deny(Middleware):
        async def on_request(self, flow: Flow) -> Action:
            return Action.DENY

    capture = Capture(options(tmp_path))
    status = await run_one(capture, before=[("guard", Deny())])
    assert status == 403
    record = records(tmp_path / "cap")[0]
    assert record["outcome"] == "denied:guard"
    assert record["response"] is None
    assert record["timings"]["ttfb_ms"] is None


async def test_a_local_answer_and_a_proxy_error_are_recorded(tmp_path: Path) -> None:
    class Local(Middleware):
        async def on_request(self, flow: Flow) -> Action:
            flow.response = Response.make(200, b"cached")
            return Action.STOP

    capture = Capture(options(tmp_path))
    await run_one(capture, before=[("cache", Local())])
    origin = Origin(fail_connect=UpstreamConnectError("refused"))
    await run_one(capture, origin)
    local, error = records(tmp_path / "cap")
    assert local["outcome"] == "local:cache" and local["response"]["body"] == "cached"
    assert error["outcome"] == "error:502" and error["response"] is None


async def test_trailers_are_recorded_and_redacted(tmp_path: Path) -> None:
    origin = Origin(protocol="h2", trailers=Headers([("x-api-key", FAKE_KEY), ("x-ok", "1")]))
    capture = Capture(options(tmp_path))
    await run_one(capture, origin, protocol="h2")
    response = records(tmp_path / "cap")[0]["response"]
    assert response["trailers"] == [["x-api-key", "[REDACTED]"], ["x-ok", "1"]]


# 5. Redaction, before anything is written.


async def test_headers_are_redacted_by_default(tmp_path: Path) -> None:
    origin = Origin(headers=[("content-type", "text/plain"), ("set-cookie", "s=1")])
    capture = Capture(options(tmp_path))
    await run_one(
        capture,
        origin,
        headers=[("Authorization", f"Bearer {FAKE_KEY}"), ("cookie", "c=1"), ("x-keep", "k")],
    )
    record = records(tmp_path / "cap")[0]
    request = dict(map(tuple, record["request"]["headers"]))
    assert request["authorization"] == "[REDACTED]" and request["cookie"] == "[REDACTED]"
    assert request["x-keep"] == "k"
    assert ["set-cookie", "[REDACTED]"] in record["response"]["headers"]


def test_json_paths() -> None:
    doc = {
        "api_key": "a",
        "items": [{"token": "t0", "n": 1}, {"token": "t1"}],
        "deep": {"x": {"token": "t2", "token2": "keep"}},
        "list": ["l0", "l1"],
    }

    def redact(path: str) -> Any:
        return JsonPath.parse(path).redact(json.loads(json.dumps(doc)), "R")[0]

    assert redact("$.api_key")["api_key"] == "R"
    assert redact("$.items[1].token")["items"] == [{"token": "t0", "n": 1}, {"token": "R"}]
    assert [i["token"] for i in redact("$.items[*].token")["items"]] == ["R", "R"]
    assert redact("$.list[0]")["list"] == ["R", "l1"]
    assert redact("$.list[9]")["list"] == ["l0", "l1"]  # out of range: nothing
    out = redact("$..token")
    assert [i["token"] for i in out["items"]] == ["R", "R"]
    assert out["deep"]["x"] == {"token": "R", "token2": "keep"}
    with pytest.raises(ValueError, match="unexpected"):
        JsonPath.parse("$.deep[x]")
    assert redact("$") == "R"
    nested = JsonPath.parse("$..a.b").redact({"a": {"a": {"b": 1}, "b": 2}}, "R")[0]
    assert nested == {"a": {"a": {"b": "R"}, "b": "R"}}
    assert JsonPath.parse("$.missing").redact({"x": 1}, "R") == ({"x": 1}, False)


async def test_json_paths_and_dotdot_in_a_request_body(tmp_path: Path) -> None:
    body = json.dumps({"api_key": FAKE_KEY, "q": "hi", "auth": {"inner": {"token": FAKE_KEY}}})
    capture = Capture(
        options(tmp_path, redact={"json_paths": ["$.api_key", "$..token"], "marker": "<gone>"})
    )
    await run_one(
        capture,
        method="POST",
        body=body.encode(),
        headers=[("content-type", "application/vnd.api+json; charset=utf-8")],
    )
    request = records(tmp_path / "cap")[0]["request"]
    assert json.loads(request["body"]) == {
        "api_key": "<gone>",
        "q": "hi",
        "auth": {"inner": {"token": "<gone>"}},
    }


async def test_json_paths_skip_non_json_and_unparsable_bodies(tmp_path: Path) -> None:
    capture = Capture(options(tmp_path, redact={"json_paths": ["$.api_key"]}))
    text = f'{{"api_key": "{FAKE_KEY}"}}'
    # Not declared as JSON: stored as it is (the wiki warns that this is silent).
    await run_one(
        capture, method="POST", body=text.encode(), headers=[("content-type", "text/plain")]
    )
    # Declared as JSON but doesn't parse: stored as it is.
    await run_one(
        capture, method="POST", body=b"{not json", headers=[("content-type", "application/json")]
    )
    plain, broken = records(tmp_path / "cap")
    assert plain["request"]["body"] == text
    assert broken["request"]["body"] == "{not json"


async def test_an_event_stream_is_stored_with_json_paths_skipped(tmp_path: Path) -> None:
    events = f'data: {{"api_key": "{FAKE_KEY}"}}\n\ndata: [DONE]\n\n'.encode()
    origin = Origin(headers=[("content-type", "text/event-stream")], body=events)
    capture = Capture(options(tmp_path, redact={"json_paths": ["$.api_key"]}))
    await run_one(capture, origin)
    record = records(tmp_path / "cap")[0]
    # Step (e) applies json_paths to data: lines; until then the stream is stored as it is.
    assert record["response"]["body"] == events.decode()
    assert record["streamed"] is False


async def test_query_params_are_redacted_and_their_names_kept(tmp_path: Path) -> None:
    capture = Capture(options(tmp_path, redact={"query_params": ["api_key", "Sig"]}))
    await run_one(
        capture, path=f"/v1/x?api_key={FAKE_KEY}&q=1&api_key=two&sig=keep&Sig=s&flag#frag"
    )
    url = records(tmp_path / "cap")[0]["request"]["url"]
    assert url == (
        "https://example.org/v1/x?api_key=[REDACTED]&q=1&api_key=[REDACTED]&sig=keep"
        "&Sig=[REDACTED]&flag#frag"
    )


# 6. Files: one per rule per UTC day, 0600 in a 0700 directory, appended, old ones untouched.


async def test_file_modes(tmp_path: Path) -> None:
    capture = Capture(options(tmp_path))
    directory = tmp_path / "cap"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700  # created at startup
    await run_one(capture)
    (path,) = directory.iterdir()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_one_file_per_rule_per_utc_day(tmp_path: Path) -> None:
    times = iter(
        [
            datetime.datetime(2026, 11, 3, 23, 59, 59, tzinfo=datetime.UTC).timestamp(),
            datetime.datetime(2026, 11, 3, 23, 59, 59, 900000, tzinfo=datetime.UTC).timestamp(),
            datetime.datetime(2026, 11, 4, 0, 0, 1, tzinfo=datetime.UTC).timestamp(),
        ]
    )
    capture = Capture(options(tmp_path, rules=[{"name": "in-api"}]), clock=lambda: next(times))
    directory = tmp_path / "cap"
    old = directory / "in-api-2026-11-01.jsonl"
    old.write_text("old\n", encoding="utf-8")
    for _ in range(3):
        await run_one(capture)
    assert sorted(p.name for p in directory.iterdir()) == [
        "in-api-2026-11-01.jsonl",
        "in-api-2026-11-03.jsonl",
        "in-api-2026-11-04.jsonl",
    ]
    assert len((directory / "in-api-2026-11-03.jsonl").read_text().splitlines()) == 2
    assert len((directory / "in-api-2026-11-04.jsonl").read_text().splitlines()) == 1
    assert old.read_text() == "old\n"  # never touched


async def test_concurrent_records_are_whole_lines(tmp_path: Path) -> None:
    capture = Capture(options(tmp_path))
    b = bench(Origin(echo=True))
    counted = Counted(capture)
    ctx = b.context("h2", [("capture", capture)])
    client = await client_for(b.proxy(ctx), "h2")
    bodies = [bytes([65 + i]) * 50000 for i in range(8)]
    await asyncio.gather(*(send(client, method="POST", body=body) for body in bodies))
    await counted.wait(8)
    await b.close()
    got = sorted(r["request"]["body"] for r in records(tmp_path / "cap"))
    assert got == sorted(body.decode() for body in bodies)


# 7. Failure fails open.


async def test_an_unwritable_dir_warns_at_startup_and_drops_records(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    blocker = tmp_path / "a-file"
    blocker.write_text("", encoding="utf-8")
    capture = Capture(Section({"dir": str(blocker / "cap"), "rules": [{"name": "r"}]}))
    assert "can't be created or written" in caplog.text  # and it loaded anyway
    caplog.clear()
    b = bench()
    counted = Counted(capture)
    ctx = b.context("http/1.1", [("capture", capture)])
    client = await client_for(b.proxy(ctx), "http/1.1")
    for _ in range(3):
        reply = await send(client)
        assert reply.status == 200 and reply.body == b"hello from origin"  # traffic unaffected
    await counted.wait(3)
    await b.close()
    assert counted.calls == [("r", "failed")] * 3
    warnings = [r for r in caplog.records if "can't be written" in r.getMessage()]
    assert len(warnings) == 1  # at most once a minute per rule
    assert Capture.on_error is None  # the default: fail open ("skip")


async def test_a_write_that_cant_take_the_lock_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mitm_proxy.middleware import capture as module

    monkeypatch.setattr(module, "LOCK_TIMEOUT", 0.01)
    capture = Capture(options(tmp_path))
    path = tmp_path / "cap" / f"all-{datetime.datetime.now(datetime.UTC):%Y-%m-%d}.jsonl"
    lock = module._lock(path)
    lock.acquire()
    try:
        await run_one(capture)
    finally:
        lock.release()
    assert not path.exists()


# 8. The metric, declared from the configuration at build and reload.


def _server_config(tmp_path: Path, rules: str, **env: str) -> Any:
    path = tmp_path / "config.yaml"
    path.write_text(
        "middleware:\n  chain: [capture]\n  config:\n"
        f"    capture: {{dir: {tmp_path / 'cap'}, rules: [{rules}]}}\n",
        encoding="utf-8",
    )
    environ = {
        "MITM_PROXY_STATE_DIR": str(tmp_path / "state"),
        "MITM_PROXY_PROXY_PEEK_TIMEOUT_MS": "50",
        **env,
    }
    return load_config(path, environ=environ)


async def test_rule_label_values_are_declared_at_build_and_reload(tmp_path: Path) -> None:
    proxy = ProxyServer(_server_config(tmp_path, "{name: one}"), connector=Origin().connector)
    assert "one" in proxy.metrics.capture_rule
    proxy.apply(_server_config(tmp_path, "{name: two}"))
    assert "two" in proxy.metrics.capture_rule
    capture = proxy.snapshot.pipeline._entries[0].middleware
    assert isinstance(capture, Capture) and capture.on_record is not None
    await proxy.stop()


# The whole path: a reverse listener (step (b)) to a local origin, over loopback.


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.asynccontextmanager
async def json_origin() -> AsyncIterator[int]:
    """A plain-HTTP/1.1 origin on 127.0.0.1 that answers JSON; yields its port."""

    async def handler(incoming: IncomingRequest, responder: Responder) -> None:
        _ = b"".join([chunk async for chunk in incoming.body])
        body = json.dumps({"answer": 42, "session": {"token": FAKE_KEY}}).encode()
        headers = Headers(
            [
                ("content-type", "application/json"),
                ("set-cookie", f"sid={FAKE_KEY}"),
                ("content-length", str(len(body))),
            ]
        )
        await responder.start(Response(200, headers), headers, end=False)
        await responder.data(body)
        await responder.end(None)

    async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await H1Server(PlainStream(reader, writer), handler, idle_timeout=5).run()

    server = await asyncio.start_server(on_client, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()


async def test_end_to_end_through_a_reverse_listener(tmp_path: Path) -> None:
    async with json_origin() as origin_port:
        listen_port = free_port()
        path = tmp_path / "config.yaml"
        path.write_text(
            f"""
proxy:
  port: {free_port()}
reverse:
  - listen: 127.0.0.1:{listen_port}
    upstream: http://127.0.0.1:{origin_port}
middleware:
  chain: [capture]
  config:
    capture:
      dir: {tmp_path / "captures"}
      redact:
        headers: [authorization, set-cookie]
        json_paths: ["$.api_key", "$..token"]
        query_params: [api_key]
      rules:
        - name: inbound-api
          direction: inbound
          host: 127.0.0.1
          path: "/v1/**"
          methods: [POST]
""",
            encoding="utf-8",
        )
        config = load_config(path, environ={"MITM_PROXY_STATE_DIR": str(tmp_path / "state")})
        proxy = ProxyServer(config)
        await proxy.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", listen_port)
            client = H1Upstream(PlainStream(reader, writer), read_timeout=5)
            body = json.dumps({"question": "what is six times seven?", "api_key": FAKE_KEY})
            reply = await send(
                client,
                "POST",
                f"/v1/answer?api_key={FAKE_KEY}&lang=en",
                body=body.encode(),
                headers=[
                    ("content-type", "application/json"),
                    ("authorization", f"Bearer {FAKE_KEY}"),
                ],
                authority="front.example",
            )
            assert reply.status == 200
            await client.close()
            for _ in range(500):
                if proxy.metrics.capture_records.values:
                    break
                await asyncio.sleep(0.01)
        finally:
            await proxy.stop()
    assert proxy.metrics.capture_records.values == {("inbound-api", "written"): 1.0}
    (path,) = (tmp_path / "captures").iterdir()
    text = path.read_text("utf-8")
    assert FAKE_KEY not in text  # nothing written before redaction
    (record,) = [json.loads(line) for line in text.splitlines()]
    assert record["rule"] == "inbound-api" and record["direction"] == "inbound"
    assert record["outcome"] == "origin" and record["streamed"] is False
    request = record["request"]
    assert request["url"] == (
        f"http://127.0.0.1:{origin_port}/v1/answer?api_key=[REDACTED]&lang=en"
    )
    assert ["authorization", "[REDACTED]"] in request["headers"]
    assert json.loads(request["body"])["api_key"] == "[REDACTED]"
    response = record["response"]
    assert json.loads(response["body"]) == {"answer": 42, "session": {"token": "[REDACTED]"}}
    assert ["set-cookie", "[REDACTED]"] in response["headers"]


async def test_a_record_of_a_body_that_decodes_too_large_omits_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mitm_proxy.middleware import capture as module

    monkeypatch.setattr(module, "MAX_DECODED_BYTES", 100)
    origin = Origin(
        headers=[("content-type", "text/plain"), ("content-encoding", "gzip")],
        body=gzip.compress(b"x" * 1000),
    )
    await run_one(Capture(options(tmp_path)), origin)
    response = records(tmp_path / "cap")[0]["response"]
    assert response["body"] is None and response["omitted"] == "oversize"
