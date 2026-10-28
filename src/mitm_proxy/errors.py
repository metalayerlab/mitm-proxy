"""Exception types. ConfigError lives in config.py, next to what raises it."""

from __future__ import annotations


class CAError(Exception):
    """The root CA can't be read, is malformed, or its key doesn't match its certificate."""


class StartupError(Exception):
    """The proxy can't start: a listener won't bind, or a middleware won't load."""


class ProtocolError(Exception):
    """A client sent a malformed request or frame."""


class UpstreamError(Exception):
    """An origin server couldn't be used for a flow."""


class UpstreamConnectError(UpstreamError):
    """The origin couldn't be reached (DNS, refused, reset)."""


class UpstreamTLSError(UpstreamError):
    """The origin's certificate failed verification, or its TLS handshake failed."""


class UpstreamTimeout(UpstreamError):
    """The origin didn't connect or answer in time."""
