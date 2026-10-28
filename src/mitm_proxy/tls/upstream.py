"""TLS towards origin servers: always verified.

There is deliberately no way to turn verification off. Internal lab CAs are
added with ``upstream.extra_ca_file``, on top of the system trust store; they
never replace it.
"""

from __future__ import annotations

import ssl

from mitm_proxy.errors import StartupError

ALPN_ANY = ("h2", "http/1.1")
ALPN_H1 = ("http/1.1",)


def upstream_context(extra_ca_file: str = "", alpn: tuple[str, ...] = ALPN_ANY) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if extra_ca_file:
        try:
            ctx.load_verify_locations(cafile=extra_ca_file)
        except (OSError, ssl.SSLError) as e:
            raise StartupError(f"upstream.extra_ca_file: cannot load {extra_ca_file}: {e}") from e
    ctx.set_alpn_protocols(list(alpn))
    # Belt and braces: these are the defaults of create_default_context.
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


class UpstreamContexts:
    """The two client contexts: one offering h2 and HTTP/1.1, one HTTP/1.1 only."""

    def __init__(self, extra_ca_file: str = "") -> None:
        self.any = upstream_context(extra_ca_file, ALPN_ANY)
        self.h1 = upstream_context(extra_ca_file, ALPN_H1)

    def for_alpn(self, alpn: tuple[str, ...]) -> ssl.SSLContext:
        return self.h1 if alpn == ALPN_H1 else self.any
