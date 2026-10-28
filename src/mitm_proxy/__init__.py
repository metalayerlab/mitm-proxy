"""Async Python MITM proxy with a self-signed CA.

HTTP/1.1 & HTTP/2, pluggable middleware for inspecting and rewriting traffic in flight.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("mitm-proxy")
except PackageNotFoundError:  # pragma: no cover - a source tree that was never installed
    __version__ = "0+unknown"

DESCRIPTION = (
    "Async Python MITM proxy with a self-signed CA — HTTP/1.1 & HTTP/2, "
    "pluggable middleware for inspecting and rewriting traffic in flight."
)

__all__ = ["DESCRIPTION", "__version__"]
