"""The intercept allowlist: which hosts are TLS-terminated and run through middleware.

Everything not listed is tunnelled untouched (fail closed). Entries are exact
names, IP literals, or ``*.suffix`` (any depth below the suffix, not the suffix
itself). Matching ignores case, IDNA-encodes names and strips a trailing dot.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass


def normalize_host(host: str) -> str:
    """The canonical form of a hostname or IP literal; ValueError if it is neither."""
    text = host.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        return ipaddress.ip_address(text).compressed
    except ValueError:
        pass
    text = text.rstrip(".").lower()
    if not text or len(text) > 253:
        raise ValueError(f"not a hostname: {host!r}")
    try:
        encoded = text.encode("idna").decode("ascii")
    except UnicodeError as e:
        raise ValueError(f"not a hostname: {host!r}") from e
    for label in encoded.split("."):
        if not label or len(label) > 63 or label.startswith("-") or label.endswith("-"):
            raise ValueError(f"not a hostname: {host!r}")
        if not all(c.isalnum() or c in "-_" for c in label):
            raise ValueError(f"not a hostname: {host!r}")
    return encoded


@dataclass(frozen=True)
class Pattern:
    text: str  # as written in the config, for messages
    exact: str | None = None
    suffix: str | None = None  # without the "*.", e.g. "example.org"

    def matches(self, host: str) -> bool:
        if self.exact is not None:
            return host == self.exact
        return self.suffix is not None and host.endswith("." + self.suffix)


def parse_pattern(text: str) -> Pattern:
    """Parse one allowlist entry; ValueError with a reason if it is invalid."""
    entry = text.strip()
    if entry in {"*", "*."}:
        raise ValueError(f"{text!r}: intercepting every host is not supported; list hosts")
    if entry.startswith("*."):
        base = entry[2:]
        if "*" in base:
            raise ValueError(f"{text!r}: '*' is only allowed as the first label")
        suffix = normalize_host(base)
        try:
            ipaddress.ip_address(suffix)
        except ValueError:
            return Pattern(text=entry, suffix=suffix)
        raise ValueError(f"{text!r}: a wildcard can't apply to an IP address")
    if "*" in entry:
        raise ValueError(f"{text!r}: '*' is only allowed as the first label")
    return Pattern(text=entry, exact=normalize_host(entry))


class Allowlist:
    def __init__(self, entries: Iterable[str]) -> None:
        self.patterns = tuple(parse_pattern(e) for e in entries)

    def match(self, host: str) -> str | None:
        """The entry that matches host (as written), or None if host is not intercepted."""
        try:
            canonical = normalize_host(host)
        except ValueError:
            return None
        for pattern in self.patterns:
            if pattern.matches(canonical):
                return pattern.text
        return None

    def __contains__(self, host: str) -> bool:
        return self.match(host) is not None
