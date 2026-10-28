"""Basic proxy authentication (``proxy.auth``)."""

from __future__ import annotations

import base64
import binascii
import hmac

CHALLENGE = 'Basic realm="mitm-proxy"'


def proxy_authorized(expected: str, header: str | None) -> bool:
    """Whether a Proxy-Authorization header carries expected ("user:password").

    No expected credentials means no authentication. The comparison takes the
    same time whatever the header holds.
    """
    if not expected:
        return True
    if not header:
        return False
    scheme, _, value = header.strip().partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        given = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError):
        return False
    return hmac.compare_digest(given, expected.encode("utf-8"))
