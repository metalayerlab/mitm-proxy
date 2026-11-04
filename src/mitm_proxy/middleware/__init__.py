"""The built-in middleware (design 0012): always available, run only when the chain names them.

This module only names them; it imports none, so reading the configuration loads no middleware.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

__all__ = ["BUILTIN"]

# name -> "module:attribute", as a middleware.available entry would say it. A
# middleware.available entry with one of these names is refused: it would shadow the built-in.
BUILTIN: Mapping[str, str] = MappingProxyType({"capture": "mitm_proxy.middleware.capture:Capture"})
