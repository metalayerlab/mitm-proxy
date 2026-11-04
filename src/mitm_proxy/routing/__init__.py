"""Routing through exits: which exit each upstream connection leaves by (design 0013).

An **exit** is an outbound proxy (HTTP ``CONNECT`` or SOCKS5 with remote name resolution) that
leaves the network in one country. With ``exits`` empty, routing is off and every upstream
connection is made directly, as before 0013. With ``exits`` set, every upstream connection goes
through an exit, and nothing ever falls back to a direct connection.

This is 0013's step 1: there are no rules (step 4) and no health model (step 5) yet. Every site
goes through the first exit in ``routing.default_via``; when that list is empty, every site is
refused (section 9). ``direct`` is a reserved exit name meaning a direct connection, allowed in
``routing.default_via`` only with ``routing.allow_direct``.

This module holds plain data and the routing decision; it never connects anywhere. The
connectors that speak to exits are in ``mitm_proxy.upstream.chain``.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass, field

from interpose.secret import Secret

__all__ = [
    "DIRECT",
    "DIRECT_ROUTE",
    "EXIT_KINDS",
    "NO_EXIT",
    "REFUSE_REASONS",
    "Exit",
    "Route",
    "Router",
    "is_ip_literal",
]

# The reserved exit name for a direct connection (today's behaviour), never an exit's name.
DIRECT = "direct"
# Each exit's kind, from its URL's scheme: an HTTP proxy, or SOCKS5 with remote name resolution.
EXIT_KINDS = ("http", "socks5h")
# Why a request was refused: the values of mitm_proxy_route_refused_total's `reason` (0013,
# section 11). Step 1 only ever refuses for `no_rule`; `down` and `connect` come with health
# (step 5).
REFUSE_REASONS = ("down", "no_rule", "connect")
# The `exit` label of a refusal that names no exit (no rule matched and no default): empty, as
# Prometheus treats an absent label. Never an exit's name: those are never empty.
NO_EXIT = ""

_NO_SECRET = Secret("")


@dataclass(frozen=True)
class Exit:
    """One exit, as the ``exits`` setting describes it (0013, section 2).

    ``health`` and ``retry`` are stored but not yet acted on: they belong to the health model
    (step 5) and retrying a blocked page (step 10).
    """

    name: str
    kind: str  # one of EXIT_KINDS
    host: str  # the exit's own host: the only name the proxy resolves when routing is on
    port: int
    country: str = ""
    health: str = ""
    retry: bool = True
    auth: Secret = field(default=_NO_SECRET, repr=False)

    @property
    def url(self) -> str:
        """The exit's URL, as configured (no user info: there never is any)."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.kind}://{host}:{self.port}"

    @property
    def health_service(self) -> str:
        """The Kubernetes Service whose readiness is this exit's health (step 5): ``health``, or
        the host part of the URL up to the first dot."""
        return self.health or self.host.split(".", 1)[0]

    def credentials(self) -> tuple[str, str] | None:
        """``(user, password)`` from ``auth``, or None when the exit needs no login."""
        if not self.auth:
            return None
        user, _, password = self.auth.reveal().partition(":")
        return user, password


@dataclass(frozen=True)
class Route:
    """Where one site's upstream connections go.

    ``exit`` None with ``refused`` None is a direct connection: routing is off, or ``direct`` was
    chosen. ``refused`` set means the request must be refused and never forwarded (0013, section
    9); it is a REFUSE_REASONS value, and ``detail`` says why in a few words, never a path.
    """

    exit: Exit | None = None
    refused: str | None = None
    detail: str = ""

    @property
    def label(self) -> str:
        """The exit's name for logs and metrics: an exit's name, ``direct``, or NO_EXIT."""
        if self.exit is not None:
            return self.exit.name
        return NO_EXIT if self.refused is not None else DIRECT


DIRECT_ROUTE = Route()


class Router:
    """The routing decision for each site: step 1 of 0013.

    ``exits`` empty: routing is off, every site is direct. Otherwise every site goes through the
    first entry of ``default_via``, an exit or (only when the configuration allows it) ``direct``;
    with ``default_via`` empty, every site is refused. There is no failover until step 5: a
    failure to reach the exit fails the request.
    """

    def __init__(self, exits: Iterable[Exit] = (), default_via: Iterable[str] = ()) -> None:
        self.exits = {e.name: e for e in exits}
        self.default_via = tuple(default_via)
        if not self.exits:
            self._default = DIRECT_ROUTE
        elif not self.default_via:
            self._default = Route(
                refused="no_rule",
                detail="no rule matched and routing.default_via is empty",
            )
        elif self.default_via[0] == DIRECT:
            self._default = DIRECT_ROUTE
        else:
            # load_config has checked every name in default_via is an exit.
            self._default = Route(self.exits[self.default_via[0]])

    @property
    def enabled(self) -> bool:
        """Routing is on when ``exits`` is not empty."""
        return bool(self.exits)

    def route(self, host: str) -> Route:
        """The route for a site. Step 1 has no rules, so the host doesn't change it yet."""
        return self._default


def is_ip_literal(host: str) -> bool:
    """True for an IPv4 or IPv6 address (brackets allowed), which is passed on as it is."""
    text = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True
