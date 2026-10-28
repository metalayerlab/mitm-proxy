"""Header normalization, so that middleware sees one representation whatever the protocol.

Connection-specific headers (forbidden in HTTP/2, RFC 9113 §8.2.2) are removed on
the way in, from both protocols. Host / :authority become Request.authority, and
the other pseudo-headers become Request fields. Framing headers are written again
on the way out by framing.py; the converters here put the rest back in each
protocol's shape.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit

from mitm_proxy.errors import ProtocolError
from mitm_proxy.http.flow import Request, Response
from mitm_proxy.http.headers import Headers

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-connection",
        "transfer-encoding",
        "upgrade",
        "te",
        "http2-settings",
        "proxy-authorization",  # credentials for this proxy, never for the origin
        "proxy-authenticate",
    }
)


def normalize(headers: Headers) -> Headers:
    """A copy without connection-specific headers or Host (TE: trailers is kept)."""
    named = {
        token.strip().lower()
        for value in headers.get_all("connection")
        for token in value.split(",")
        if token.strip()
    }
    out = Headers()
    for name, value in headers:
        if name == "te" and value.strip().lower() == "trailers":
            out.add(name, "trailers")
        elif name in HOP_BY_HOP or name in named or name == "host" or name.startswith(":"):
            continue
        else:
            out.add(name, value)
    return out


def parse_target(target: str) -> tuple[str | None, str | None, str]:
    """(scheme, authority, path) of an h1 request target; scheme/authority None for origin-form."""
    if target == "*" or target.startswith("/"):
        return None, None, target
    parts = urlsplit(target)
    if parts.scheme in ("http", "https") and parts.netloc:
        if "@" in parts.netloc:
            raise ProtocolError("userinfo in a request target is not allowed")
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        return parts.scheme, parts.netloc, path
    raise ProtocolError(f"unsupported request target {target!r}")


def request_from_h2(raw: Iterable[tuple[bytes, bytes]]) -> Request:
    pseudo: dict[str, str] = {}
    regular = Headers()
    for name_b, value_b in raw:
        name, value = name_b.decode("latin-1"), value_b.decode("latin-1")
        if name.startswith(":"):
            pseudo[name] = value
        else:
            regular.add(name, value)
    method = pseudo.get(":method")
    if not method:
        raise ProtocolError("h2 request without :method")
    authority = pseudo.get(":authority") or regular.get("host") or ""
    return Request(
        method=method,
        scheme=pseudo.get(":scheme", "https"),
        authority=authority,
        path=pseudo.get(":path", "/"),
        headers=normalize(regular),
    )


def _joined_cookies(headers: Headers) -> Headers:
    """h2 may split Cookie into several fields; h1 needs one (RFC 9113 §8.2.3)."""
    cookies = headers.get_all("cookie")
    if len(cookies) <= 1:
        return headers
    out = headers.copy()
    out.set("cookie", "; ".join(cookies))
    return out


def h1_request_headers(request: Request, framed: Headers) -> list[tuple[bytes, bytes]]:
    """Host first, then the (already framed) headers."""
    out = Headers([("host", request.authority)])
    for name, value in _joined_cookies(framed):
        out.add(name, value)
    return out.to_bytes()


def h2_request_headers(request: Request, framed: Headers) -> list[tuple[bytes, bytes]]:
    pseudo = [
        (b":method", request.method.encode("latin-1")),
        (b":scheme", request.scheme.encode("latin-1")),
        (b":authority", request.authority.encode("latin-1")),
        (b":path", request.path.encode("latin-1")),
    ]
    return [*pseudo, *framed.to_bytes()]


def h1_response_headers(framed: Headers) -> list[tuple[bytes, bytes]]:
    return _joined_cookies(framed).to_bytes()


def h2_response_headers(response: Response, framed: Headers) -> list[tuple[bytes, bytes]]:
    return [(b":status", str(response.status).encode()), *framed.to_bytes()]


def response_from_h2(raw: Iterable[tuple[bytes, bytes]]) -> Response:
    status = 0
    regular = Headers()
    for name_b, value_b in raw:
        if name_b == b":status":
            status = int(value_b)
        elif not name_b.startswith(b":"):
            regular.add(name_b.decode("latin-1"), value_b.decode("latin-1"))
    if not 100 <= status <= 999:
        raise ProtocolError("h2 response without a valid :status")
    return Response(status=status, headers=normalize(regular))
