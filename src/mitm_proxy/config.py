"""Configuration.

Every setting can come from four places, and the first one that has it wins:

1. ``MITM_PROXY_<NAME>_FILE``: a file that holds the value (Docker / Kubernetes secrets)
2. ``MITM_PROXY_<NAME>``: an environment variable
3. the YAML config file (``MITM_PROXY_CONFIG``, default ``/etc/mitm-proxy/config.yaml``)
4. the default in SETTINGS

``<NAME>`` is the dotted key in upper case, with the dots as underscores:
``listen.port`` is ``MITM_PROXY_LISTEN_PORT``.

The engine (lookup, strict coercion, ``check``, ``Secret``) is interpose's
(design 0008); this module holds mitm-proxy's settings and its own rules. A
rule about one setting is its ``check=``; a rule that spans settings is in
load_config.

A list setting is a YAML list, or a comma-separated string (in an environment
variable or a file); its default is ``""`` (empty). A section setting
(``middleware.config``) is a YAML mapping, read from the config file or from
``MITM_PROXY_<NAME>_FILE`` (a YAML file holding the whole section), never from a
plain variable; it resolves to an interpose ``Section``, read-only at every
depth, in which a ``{secret_file: <path>}`` leaf is a ``Secret`` (design 0012).
A ``secret=True`` setting resolves to a ``Secret``: call ``reveal()`` where the
value is used, never ``str()`` (which gives the mask).

To add a setting, add it to SETTINGS and to Config, and read it in load_config.
The wiki's settings table is generated from SETTINGS (``scripts/gen-wiki.py``),
so write its help as you want users to read it. Values are never logged;
only where each one came from is.

The contract with the template-owned files (cli.py, scripts/gen-wiki.py): this
module exports SETTINGS (each item has key, env, default, help, secret,
is_list), CONFIG_ENV, Config (with sources), ConfigError and
load_config(path=None, environ=None).

A Config is immutable: the running proxy holds one as a snapshot and replaces
it whole (design 0002), so nothing may cache a field it read at startup.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import cast
from urllib.parse import urlsplit

from interpose.secret import Secret
from interpose.settings import (
    ConfigError,
    Section,
    Setting,
    Settings,
    Value,
    one_of,
    port,
    positive,
)

from mitm_proxy.scope import parse_pattern

__all__ = [
    "CONFIG_ENV",
    "DEFAULT_CONFIG_PATH",
    "ENV_PREFIX",
    "LOG_LEVELS",
    "SETTINGS",
    "Config",
    "ConfigError",
    "ReverseListener",
    "Secret",
    "Section",
    "Value",
    "load_config",
]

ENV_PREFIX = "MITM_PROXY_"
DEFAULT_CONFIG_PATH = Path("/etc/mitm-proxy/config.yaml")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_REF = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_][\w.]*$")


# Single-setting rules, for `check=`. Each gets the coerced value and raises ValueError with a
# reason; interpose prefixes the key (and masks a secret's value in the reason).


def _items(value: Value) -> tuple[str, ...]:
    if not isinstance(value, tuple):  # pragma: no cover - a list setting is a tuple
        raise ValueError("expected a list")
    items = tuple(item for item in value if isinstance(item, str))
    if len(items) != len(value):  # pragma: no cover - a list setting's items are str
        raise ValueError("expected a list of strings")
    return items


def _host_patterns(value: Value) -> None:
    """intercept.hosts: every entry is an allowlist pattern."""
    for entry in _items(value):
        parse_pattern(entry)


def _parse_available(value: Value) -> dict[str, str]:
    """middleware.available: `name=module:attribute` entries, each name once."""
    available: dict[str, str] = {}
    for entry in _items(value):
        name, sep, ref = entry.partition("=")
        name, ref = name.strip(), ref.strip()
        if not sep or not _NAME.match(name) or not _REF.match(ref):
            raise ValueError(f"{entry!r} is not name=module:attribute")
        if name in available:
            raise ValueError(f"{name!r} is defined twice")
        available[name] = ref
    return available


def _check_available(value: Value) -> None:
    _parse_available(value)


def _no_repeats(value: Value) -> None:
    """middleware.chain: a middleware runs once."""
    items = _items(value)
    if len(set(items)) != len(items):
        raise ValueError("a middleware is listed twice")


def _middleware_config(value: Value) -> None:
    """middleware.config: each entry, `<name>: {...}`, is a mapping for that middleware."""
    if not isinstance(value, Section):  # pragma: no cover - a section setting is a Section
        raise ValueError("expected a mapping")
    for name, entry in value.items():
        if not isinstance(entry, Section):
            raise ValueError(
                f"{name!r} is not a mapping: give the middleware's options as `{name}: {{...}}`"
            )


def _user_password(value: Value) -> None:
    """proxy.auth: empty, or `user:password` with a user."""
    if not isinstance(value, str):  # pragma: no cover - a str setting is a str
        raise ValueError("expected user:password")
    if value:
        user, sep, _ = value.partition(":")
        if not sep or not user:
            raise ValueError("expected user:password")


@dataclass(frozen=True)
class ReverseListener:
    """One reverse-proxy listener and its fixed upstream (design 0012).

    ``upstream_authority`` is what the origin is told as ``Host`` (the upstream host, with its
    port when the configuration gave one); ``upstream_host`` is the name to connect to and the SNI.
    """

    listen_host: str
    listen_port: int
    upstream_host: str
    upstream_port: int
    upstream_tls: bool
    upstream_authority: str


def _parse_listen(where: str, value: str) -> tuple[str, int]:
    """`host:port`, or `[ipv6]:port`; the port is required and 1-65535. Raises ConfigError."""
    text = value.strip()
    if text.startswith("["):
        end = text.find("]")
        if end == -1 or not text[end + 1 :].startswith(":"):
            raise ConfigError(f"{where}: expected [ipv6]:port, got {value!r}")
        host, port_text = text[1:end], text[end + 2 :]
    else:
        host, sep, port_text = text.rpartition(":")
        if not sep:
            raise ConfigError(f"{where}: expected host:port, got {value!r}")
    if not host:
        raise ConfigError(f"{where}: no host in {value!r}")
    if not port_text.isdigit() or not 0 < int(port_text) <= 65535:
        raise ConfigError(f"{where}: {port_text!r} is not a port 1-65535")
    return host, int(port_text)


def _parse_upstream(where: str, value: str) -> tuple[str, int, bool, str]:
    """`http://` or `https://`, a host and an optional port, with no path, query or user info.

    Returns (host, port, tls, authority). Raises ConfigError naming ``where``.
    """
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https"):
        raise ConfigError(f"{where}: must start with http:// or https://, got {value!r}")
    if parts.path or parts.query or parts.fragment:
        raise ConfigError(f"{where}: just a host and an optional port, no path or query: {value!r}")
    try:
        host, port = parts.hostname, parts.port
    except ValueError as e:
        raise ConfigError(f"{where}: {e}") from None
    if parts.username is not None or parts.password is not None:
        raise ConfigError(f"{where}: no user info in the upstream URL")
    if not host:
        raise ConfigError(f"{where}: no host in {value!r}")
    tls = parts.scheme == "https"
    return host, port if port is not None else (443 if tls else 80), tls, parts.netloc


def _parse_reverse(sections: tuple[Section, ...]) -> tuple[ReverseListener, ...]:
    """reverse: validate each item and that listen addresses are unique (design 0012)."""
    listeners: list[ReverseListener] = []
    seen: dict[tuple[str, int], int] = {}
    for i, item in enumerate(sections):
        host, port = _parse_listen(f"reverse[{i}].listen", cast(str, item["listen"]))
        key = (host.lower(), port)
        if key in seen:
            raise ConfigError(
                f"reverse[{i}].listen: {host}:{port} is already a listen address "
                f"(reverse[{seen[key]}])"
            )
        seen[key] = i
        up_host, up_port, up_tls, up_authority = _parse_upstream(
            f"reverse[{i}].upstream", cast(str, item["upstream"])
        )
        listeners.append(ReverseListener(host, port, up_host, up_port, up_tls, up_authority))
    return tuple(listeners)


SETTINGS = Settings(
    ENV_PREFIX,
    (
        Setting(
            "log_level",
            "INFO",
            "Log level: `DEBUG`, `INFO`, `WARNING` or `ERROR`.",
            check=one_of(*LOG_LEVELS),
        ),
        Setting("listen.host", "127.0.0.1", "Address the admin HTTP API binds to."),
        Setting("listen.port", 8080, "Port the admin HTTP API listens on.", check=port),
        Setting("state_dir", "/var/lib/mitm-proxy", "Directory for persistent state (the CA)."),
        Setting(
            "proxy.host",
            "127.0.0.1",
            "Address the proxy listens on. Anything but loopback without `proxy.auth` "
            "is an open proxy.",
        ),
        Setting("proxy.port", 3128, "Port the proxy listens on.", check=port),
        Setting(
            "proxy.auth",
            "",
            "`user:password` for Basic proxy authentication. Empty: no authentication.",
            secret=True,
            check=_user_password,
        ),
        Setting(
            "proxy.peek_timeout_ms",
            1000,
            "How long to wait for a TLS ClientHello after CONNECT before relaying the tunnel raw.",
            check=positive,
        ),
        Setting(
            "proxy.idle_timeout_ms",
            120000,
            "Close a client connection or tunnel after this long with no traffic.",
            check=positive,
        ),
        Setting(
            "proxy.shutdown_grace_ms",
            10000,
            "On SIGTERM, how long in-flight connections get to finish.",
            check=positive,
        ),
        Setting(
            "proxy.access_log",
            False,
            "Log one line per intercepted flow (`mitm_proxy.flows`): sizes, timings and how it "
            "ended.",
        ),
        Setting(
            "proxy.access_log_relayed",
            False,
            "Log one line per flow relayed without interception (`mitm_proxy.relayed`): tunnels "
            "to unlisted hosts and plain HTTP to them. Records every destination.",
        ),
        Setting(
            "proxy.server_timing",
            False,
            "Add a `Server-Timing` header (connect, origin, middleware, proxy times) to every "
            "intercepted response, for browser devtools. Tells pages they were intercepted.",
        ),
        Setting(
            "intercept.hosts",
            "",
            "The allowlist: hosts to intercept (`example.org`, `*.example.org`, IP literals). "
            "Everything else is tunnelled untouched. Empty: intercept nothing.",
            is_list=True,
            check=_host_patterns,
        ),
        Setting(
            "reverse",
            "",
            "Reverse-proxy listeners. Each item has `listen` (the `host:port` this proxy binds) "
            "and `upstream` (an `http://` or `https://` host, optional port) it forwards to; every "
            "request on a listener is intercepted and sent to its upstream. From the config file "
            "or `_FILE`, or the indexed variables `MITM_PROXY_REVERSE_<N>_LISTEN` and "
            "`_<N>_UPSTREAM`. Empty: no reverse listeners.",
            is_section=True,
            is_list=True,
            fields=(
                Setting(
                    "listen",
                    "",
                    "The `host:port` this listener binds (`[::1]:8081` for IPv6).",
                    required=True,
                ),
                Setting(
                    "upstream",
                    "",
                    "Where it forwards: `http://` or `https://`, a host and an optional port, "
                    "with no path, query or user info.",
                    required=True,
                ),
            ),
        ),
        Setting(
            "middleware.available",
            "",
            "Installed middleware that may run, as `name=module:attribute`.",
            is_list=True,
            check=_check_available,
        ),
        Setting(
            "middleware.chain",
            "",
            "The middleware to run, by name, in order.",
            is_list=True,
            check=_no_repeats,
        ),
        Setting(
            "middleware.config",
            "",
            "Options for each middleware, by name: `<name>: {...}`, passed to its class's "
            "constructor. A `{secret_file: <path>}` leaf is read as a secret. From the config "
            "file or `_FILE`, never a plain variable.",
            is_section=True,
            check=_middleware_config,
        ),
        Setting(
            "middleware.hook_timeout_ms",
            5000,
            "A middleware hook that runs longer than this has failed, and is skipped for the flow.",
            check=positive,
        ),
        Setting(
            "body.max_buffer_bytes",
            67108864,
            "The largest body buffered for middleware. A larger body skips middleware and is "
            "streamed through.",
            check=positive,
        ),
        Setting(
            "ca.cert",
            "",
            "Root CA certificate (PEM), usually via `_FILE`. Empty: `<state_dir>/ca/ca.pem`, "
            "generated on first start.",
        ),
        Setting(
            "ca.key",
            "",
            "Root CA private key (PEM). Set together with `ca.cert`, or not at all.",
            secret=True,
        ),
        Setting(
            "ca.common_name",
            "mitm-proxy CA",
            "Common name of a newly generated root CA, as clients' trust stores show it.",
        ),
        Setting("ca.leaf_cache_size", 1024, "Leaf certificates kept in memory.", check=positive),
        Setting(
            "upstream.extra_ca_file",
            "",
            "PEM bundle of extra CAs trusted for origin servers, on top of the system store.",
        ),
        Setting(
            "upstream.connect_timeout_ms",
            10000,
            "Connecting to an origin (TCP and TLS), including any wait for a free pool slot.",
            check=positive,
        ),
        Setting(
            "upstream.idle_timeout_ms",
            60000,
            "Close a pooled upstream connection after this long unused.",
            check=positive,
        ),
        Setting(
            "upstream.max_connections_per_host",
            6,
            "The most upstream connections kept to one host, port and SNI.",
            check=positive,
        ),
    ),
    default_path=DEFAULT_CONFIG_PATH,
)
CONFIG_ENV = SETTINGS.config_env

_NO_SECRET = Secret("")
_NO_OPTIONS: Mapping[str, Section] = MappingProxyType({})


@dataclass(frozen=True)
class Config:
    log_level: str
    host: str
    port: int
    state_dir: Path
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 3128
    proxy_auth: Secret = _NO_SECRET
    peek_timeout_ms: int = 1000
    idle_timeout_ms: int = 120000
    shutdown_grace_ms: int = 10000
    access_log: bool = False
    access_log_relayed: bool = False
    server_timing: bool = False
    intercept_hosts: tuple[str, ...] = ()
    reverse: tuple[ReverseListener, ...] = ()
    # name -> "module:attribute"
    middleware_available: Mapping[str, str] = field(default_factory=dict)
    middleware_chain: tuple[str, ...] = ()
    # name -> that middleware's options, passed to its constructor (design 0012). Read-only.
    middleware_config: Mapping[str, Section] = _NO_OPTIONS
    hook_timeout_ms: int = 5000
    max_buffer_bytes: int = 67108864
    ca_cert: str = ""
    ca_key: Secret = _NO_SECRET
    ca_common_name: str = "mitm-proxy CA"
    leaf_cache_size: int = 1024
    upstream_extra_ca_file: str = ""
    upstream_connect_timeout_ms: int = 10000
    upstream_idle_timeout_ms: int = 60000
    upstream_max_connections_per_host: int = 6
    # Setting key -> the resolved value, for display; secret settings hold a Secret.
    values: Mapping[str, Value] = field(default_factory=dict, repr=False)
    # Setting key -> where its value came from ("default", "file", an env var name).
    sources: Mapping[str, str] = field(default_factory=dict)


def load_config(path: Path | None = None, environ: Mapping[str, str] | None = None) -> Config:
    """Load the configuration. A missing config file is not an error: defaults apply."""
    r = SETTINGS.resolve(path, environ)
    # SETTINGS has coerced every value to its setting's type; the typed accessors return it as
    # that type, and a secret=True one only through r.secret() (a Secret: reveal() where used).
    # one_of gives log_level the declared spelling (upper case, as logging wants it).

    # Rules that span settings.
    available = _parse_available(r.list("middleware.available"))
    chain = r.list("middleware.chain")
    for name in chain:
        if name == "log" and name not in available:
            raise ConfigError(
                "middleware.chain: the built-in 'log' middleware was replaced by the "
                "proxy.access_log setting; remove 'log' from the chain and set that instead"
            )
        if name not in available:
            raise ConfigError(
                f"middleware.chain: {name!r} is not in middleware.available "
                f"(known: {', '.join(sorted(available)) or 'none'})"
            )

    # Options for a middleware that can't run are a mistake (a typo, or one left behind).
    # There are no built-in middleware yet: when there are, they are available too.
    options: dict[str, Section] = {}
    for name, entry in r.section("middleware.config").items():
        if name not in available:
            raise ConfigError(
                f"middleware.config: {name!r} is not in middleware.available "
                f"(known: {', '.join(sorted(available)) or 'none'})"
            )
        if not isinstance(entry, Section):  # pragma: no cover - its check refused it
            raise ConfigError(f"middleware.config.{name}: expected a mapping")
        options[name] = entry

    reverse = _parse_reverse(r.sections("reverse"))

    ca_cert, ca_key = r.str("ca.cert"), r.secret("ca.key")
    if bool(ca_cert) != bool(ca_key):
        raise ConfigError("ca.cert and ca.key: set both, or neither")

    return Config(
        log_level=r.str("log_level"),
        host=r.str("listen.host"),
        port=r.int("listen.port"),
        state_dir=Path(r.str("state_dir")),
        proxy_host=r.str("proxy.host"),
        proxy_port=r.int("proxy.port"),
        proxy_auth=r.secret("proxy.auth"),
        peek_timeout_ms=r.int("proxy.peek_timeout_ms"),
        idle_timeout_ms=r.int("proxy.idle_timeout_ms"),
        shutdown_grace_ms=r.int("proxy.shutdown_grace_ms"),
        access_log=r.bool("proxy.access_log"),
        access_log_relayed=r.bool("proxy.access_log_relayed"),
        server_timing=r.bool("proxy.server_timing"),
        intercept_hosts=r.list("intercept.hosts"),
        reverse=reverse,
        middleware_available=available,
        middleware_chain=chain,
        middleware_config=MappingProxyType(options),
        hook_timeout_ms=r.int("middleware.hook_timeout_ms"),
        max_buffer_bytes=r.int("body.max_buffer_bytes"),
        ca_cert=ca_cert,
        ca_key=ca_key,
        ca_common_name=r.str("ca.common_name"),
        leaf_cache_size=r.int("ca.leaf_cache_size"),
        upstream_extra_ca_file=r.str("upstream.extra_ca_file"),
        upstream_connect_timeout_ms=r.int("upstream.connect_timeout_ms"),
        upstream_idle_timeout_ms=r.int("upstream.idle_timeout_ms"),
        upstream_max_connections_per_host=r.int("upstream.max_connections_per_host"),
        values=dict(r.values),
        sources=dict(r.sources),
    )
