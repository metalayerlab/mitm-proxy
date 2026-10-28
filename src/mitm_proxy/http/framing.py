"""How each outgoing message's body is framed: Content-Length, chunked, or no body.

The choice is made once per message from what is *registered* for the flow, never
from what a hook did: a flow with mutation-capable middleware is always sent
length-independently (h1 chunked; h2 DATA frames with no content-length),
because chunks may be forwarded before a later one changes. Responses that can't
carry a body (to HEAD, 1xx, 204, 304) never get framing headers added: that is a
separate rule here, not a side effect of the general one.
"""

from __future__ import annotations

import enum

from mitm_proxy.http.headers import Headers


class Framing(enum.Enum):
    NONE = "none"  # no body: headers left as they were (a HEAD response keeps its length)
    LENGTH = "length"
    CHUNKED = "chunked"  # h1 Transfer-Encoding: chunked; h2 DATA frames, no content-length


def response_allows_body(request_method: str, status: int) -> bool:
    return not (request_method == "HEAD" or 100 <= status < 200 or status in (204, 304))


def declared_length(headers: Headers) -> int | None:
    value = headers.get("content-length")
    if value is None:
        return None
    try:
        length = int(value.split(",")[0].strip())
    except ValueError:
        return None
    return length if length >= 0 else None


def plan(
    *,
    has_body: bool,
    mutation_capable: bool,
    buffered_length: int | None,
    declared: int | None,
) -> tuple[Framing, int | None]:
    """The framing for one message, and its length when that is LENGTH."""
    if not has_body:
        return Framing.NONE, None
    if mutation_capable:
        return Framing.CHUNKED, None
    if buffered_length is not None:
        # Middleware may have replaced the body by assignment: send what we actually have.
        return Framing.LENGTH, buffered_length
    if declared is not None:
        return Framing.LENGTH, declared
    return Framing.CHUNKED, None


def apply(headers: Headers, framing: Framing, length: int | None, *, h1: bool) -> Headers:
    """A copy of headers with the framing headers for one protocol."""
    out = headers.copy()
    out.remove("transfer-encoding")
    if framing is Framing.LENGTH:
        out.set("content-length", str(length))
    elif framing is Framing.CHUNKED:
        out.remove("content-length")
        if h1:
            out.set("transfer-encoding", "chunked")
    return out
