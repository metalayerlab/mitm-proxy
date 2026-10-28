"""The one place that decides: intercept a tunnel, or relay it untouched.

Scope is checked first, so an out-of-scope tunnel costs no peek. In scope, the
first client bytes are peeked (kept for replay) and parsed as a TLS ClientHello;
anything that isn't TLS (or sends nothing: server-speaks-first protocols), or
asks for an unlisted SNI, is relayed raw. No certificate is minted for a host
nobody listed.
"""

from __future__ import annotations

import asyncio
import enum
from dataclasses import dataclass

from mitm_proxy.scope import Allowlist
from mitm_proxy.streams import ByteStream
from mitm_proxy.tls.clienthello import ClientHello, NotTLS, parse_client_hello

MAX_PEEK = 64 * 1024


class Decision(enum.Enum):
    INTERCEPT = "intercept"
    RELAY_OUT_OF_SCOPE = "out_of_scope"
    RELAY_NOT_TLS = "not_tls"
    RELAY_SNI_OUT_OF_SCOPE = "sni_out_of_scope"


@dataclass(frozen=True)
class Peeked:
    data: bytes  # every byte read, to replay
    hello: ClientHello | None  # None: not TLS, or nothing arrived in time


def decide(target_host: str, allowlist: Allowlist, peeked: Peeked | None) -> Decision:
    """The intercept-or-relay decision. peeked is None before peeking (scope only)."""
    if allowlist.match(target_host) is None:
        return Decision.RELAY_OUT_OF_SCOPE
    if peeked is None:
        return Decision.INTERCEPT  # in scope so far: peek next
    if peeked.hello is None:
        return Decision.RELAY_NOT_TLS
    sni = peeked.hello.sni
    if sni is not None and allowlist.match(sni) is None:
        return Decision.RELAY_SNI_OUT_OF_SCOPE
    return Decision.INTERCEPT


async def peek(stream: ByteStream, timeout: float, limit: int = MAX_PEEK) -> Peeked:
    """Read until a whole ClientHello has arrived, it is clearly not TLS, or time is up."""
    data = b""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            hello = parse_client_hello(data) if data else None
        except NotTLS:
            return Peeked(data, None)
        if hello is not None:
            return Peeked(data, hello)
        remaining = deadline - loop.time()
        if remaining <= 0 or len(data) >= limit:
            return Peeked(data, None)
        try:
            chunk = await asyncio.wait_for(stream.read(), remaining)
        except TimeoutError:
            return Peeked(data, None)
        if not chunk:
            return Peeked(data, None)
        data += chunk
