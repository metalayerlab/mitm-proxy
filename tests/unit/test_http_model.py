from collections.abc import AsyncIterator

import pytest

from mitm_proxy.errors import ProtocolError
from mitm_proxy.http import framing
from mitm_proxy.http.body import BufferedBodyStream, PassthroughBody, empty
from mitm_proxy.http.flow import ClientConn, Flow, FlowType, Request, Response, split_authority
from mitm_proxy.http.framing import Framing
from mitm_proxy.http.headers import Headers
from mitm_proxy.http.normalize import (
    h1_request_headers,
    h2_request_headers,
    h2_response_headers,
    normalize,
    parse_target,
    request_from_h2,
    response_from_h2,
)


def test_headers_are_case_insensitive_and_ordered() -> None:
    h = Headers([("Content-Type", "text/plain"), ("X-A", "1"), ("x-a", "2")])
    assert h.get("content-type") == "text/plain"
    assert h.get_all("X-A") == ["1", "2"]
    assert "X-a" in h and 3 not in h
    h.set("x-a", "3")
    assert h.items() == [("content-type", "text/plain"), ("x-a", "3")]
    h.set("new", "v")
    h.add("new", "w")
    h.remove("content-type")
    assert list(h) == [("x-a", "3"), ("new", "v"), ("new", "w")]
    assert len(h) == 3 and h == h.copy() and h != Headers()
    assert h.get("missing", "d") == "d"
    assert Headers.from_bytes(h.to_bytes()) == h
    assert "x-a" in repr(h)


def test_normalize_strips_connection_specific_headers() -> None:
    h = Headers(
        [
            ("Host", "example.org"),
            ("Connection", "keep-alive, X-Hop"),
            ("X-Hop", "1"),
            ("Keep-Alive", "timeout=5"),
            ("Proxy-Connection", "keep-alive"),
            ("Proxy-Authorization", "Basic xxx"),
            ("Transfer-Encoding", "chunked"),
            ("Upgrade", "h2c"),
            ("TE", "gzip"),
            ("Accept", "*/*"),
        ]
    )
    assert normalize(h).items() == [("accept", "*/*")]
    assert normalize(Headers([("te", "Trailers")])).items() == [("te", "trailers")]


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/a?b=1", (None, None, "/a?b=1")),
        ("*", (None, None, "*")),
        ("http://example.org:8000/x?y", ("http", "example.org:8000", "/x?y")),
        ("https://example.org", ("https", "example.org", "/")),
    ],
)
def test_parse_target(target: str, expected: tuple[str | None, str | None, str]) -> None:
    assert parse_target(target) == expected


@pytest.mark.parametrize(
    "target", ["ftp://example.org/", "example.org:443", "http://u:p@example.org/"]
)
def test_parse_target_rejects(target: str) -> None:
    with pytest.raises(ProtocolError):
        parse_target(target)


def test_h2_request_round_trip() -> None:
    req = request_from_h2(
        [
            (b":method", b"GET"),
            (b":scheme", b"https"),
            (b":authority", b"example.org"),
            (b":path", b"/p"),
            (b"cookie", b"a=1"),
            (b"cookie", b"b=2"),
        ]
    )
    assert (req.method, req.scheme, req.authority, req.path) == (
        "GET",
        "https",
        "example.org",
        "/p",
    )
    assert h1_request_headers(req, req.headers) == [
        (b"host", b"example.org"),
        (b"cookie", b"a=1; b=2"),
    ]
    assert h2_request_headers(req, req.headers)[:4] == [
        (b":method", b"GET"),
        (b":scheme", b"https"),
        (b":authority", b"example.org"),
        (b":path", b"/p"),
    ]


def test_h2_request_needs_a_method() -> None:
    with pytest.raises(ProtocolError):
        request_from_h2([(b":path", b"/")])


def test_h2_response_round_trip() -> None:
    resp = response_from_h2([(b":status", b"404"), (b"content-type", b"text/plain")])
    assert resp.status == 404
    assert h2_response_headers(resp, resp.headers) == [
        (b":status", b"404"),
        (b"content-type", b"text/plain"),
    ]
    with pytest.raises(ProtocolError):
        response_from_h2([(b"content-type", b"x")])


@pytest.mark.parametrize(
    ("authority", "expected"),
    [
        ("example.org", ("example.org", 443)),
        ("example.org:8443", ("example.org", 8443)),
        ("[2001:db8::1]:8443", ("2001:db8::1", 8443)),
        ("[2001:db8::1]", ("2001:db8::1", 443)),
        ("2001:db8::1", ("2001:db8::1", 443)),
        ("[broken", ("[broken", 443)),
    ],
)
def test_split_authority(authority: str, expected: tuple[str, int]) -> None:
    assert split_authority(authority, 443) == expected


def test_flow_shortcuts_and_read_only_protocol() -> None:
    req = Request("GET", "http", "example.org", "/")
    flow = Flow(
        type=FlowType.HTTP, protocol="h2", client_conn=ClientConn(("192.0.2.1", 1)), request=req
    )
    assert (flow.host, flow.port, flow.method, flow.protocol) == ("example.org", 80, "GET", "h2")
    assert flow.id in repr(flow)
    with pytest.raises(AttributeError):
        flow.protocol = "http/1.1"  # type: ignore[misc]  # asserting it is read-only


def test_response_make() -> None:
    resp = Response.make(403, b"no")
    assert resp.headers.get("content-type") == "text/plain; charset=utf-8"
    assert Response.make(204).headers.items() == []


async def test_buffered_body_yields_one_chunk() -> None:
    body = BufferedBodyStream(b"abc")
    assert [c async for c in body] == [b"abc"]
    assert await body.read_all() == b"abc" and body.is_complete() and len(body) == 3
    assert [c async for c in BufferedBodyStream()] == []
    assert "3 bytes" in repr(body)


async def test_passthrough_body_iterates_once() -> None:
    async def source() -> AsyncIterator[bytes]:
        yield b"a"
        yield b""
        yield b"b"

    body = PassthroughBody(source())
    assert not body.is_complete()
    assert [c async for c in body] == [b"a", b"b"]
    assert body.is_complete()
    with pytest.raises(RuntimeError):
        body.__aiter__()
    with pytest.raises(RuntimeError):
        await body.read_all()
    assert [c async for c in empty()] == []


@pytest.mark.parametrize(
    ("method", "status", "allowed"),
    [
        ("HEAD", 200, False),
        ("GET", 101, False),
        ("GET", 204, False),
        ("GET", 304, False),
        ("GET", 200, True),
    ],
)
def test_response_allows_body(method: str, status: int, allowed: bool) -> None:
    assert framing.response_allows_body(method, status) is allowed


def test_plan_rules() -> None:
    assert framing.plan(has_body=False, mutation_capable=True, buffered_length=5, declared=5) == (
        Framing.NONE,
        None,
    )
    assert framing.plan(has_body=True, mutation_capable=True, buffered_length=5, declared=5) == (
        Framing.CHUNKED,
        None,
    )
    assert framing.plan(has_body=True, mutation_capable=False, buffered_length=7, declared=5) == (
        Framing.LENGTH,
        7,
    )
    assert framing.plan(
        has_body=True, mutation_capable=False, buffered_length=None, declared=5
    ) == (Framing.LENGTH, 5)
    assert framing.plan(
        has_body=True, mutation_capable=False, buffered_length=None, declared=None
    ) == (Framing.CHUNKED, None)


def test_apply_framing_per_protocol() -> None:
    h = Headers([("content-length", "5"), ("x", "y")])
    assert framing.apply(h, Framing.CHUNKED, None, h1=True).items() == [
        ("x", "y"),
        ("transfer-encoding", "chunked"),
    ]
    assert framing.apply(h, Framing.CHUNKED, None, h1=False).items() == [("x", "y")]
    assert framing.apply(h, Framing.LENGTH, 9, h1=True).get("content-length") == "9"
    head = framing.apply(h, Framing.NONE, None, h1=True)
    assert head.get("content-length") == "5" and "transfer-encoding" not in head


@pytest.mark.parametrize(
    ("value", "expected"), [("12", 12), ("12, 12", 12), ("x", None), ("-1", None)]
)
def test_declared_length(value: str, expected: int | None) -> None:
    assert framing.declared_length(Headers([("content-length", value)])) == expected
    assert framing.declared_length(Headers()) is None
