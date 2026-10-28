"""Configuration.

Every setting can come from four places, and the first one that has it wins:

1. ``MITM_PROXY_<NAME>_FILE``: a file that holds the value (Docker / Kubernetes secrets)
2. ``MITM_PROXY_<NAME>``: an environment variable
3. the YAML config file (``MITM_PROXY_CONFIG``, default ``/etc/mitm-proxy/config.yaml``)
4. the default in SETTINGS

``<NAME>`` is the dotted key in upper case, with the dots as underscores:
``listen.port`` is ``MITM_PROXY_LISTEN_PORT``.

A list setting is a YAML list, or a comma-separated string (in an environment
variable or a file); its default is ``""`` (empty).

To add a setting, add it to SETTINGS and to Config, and read it in load_config.
The wiki's settings table is generated from SETTINGS (``scripts/gen-wiki.py``),
so write its help as you want users to read it. Values are never logged;
only where each one came from is.

A Config is immutable: the running proxy holds one as a snapshot and replaces
it whole (design 0002), so nothing may cache a field it read at startup.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from mitm_proxy.scope import parse_pattern

ENV_PREFIX = "MITM_PROXY_"
CONFIG_ENV = f"{ENV_PREFIX}CONFIG"
DEFAULT_CONFIG_PATH = Path("/etc/mitm-proxy/config.yaml")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
# The built-in middleware, always available under this name.
BUILTIN_MIDDLEWARE = {"log": "mitm_proxy.middleware.log:FlowLogger"}

Value = str | int | bool | tuple[str, ...]

_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_REF = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_][\w.]*$")


class ConfigError(Exception):
    """The configuration can't be read, or a value in it is invalid."""


@dataclass(frozen=True)
class Setting:
    key: str
    default: str | int | bool
    help: str
    secret: bool = False
    is_list: bool = False

    @property
    def env(self) -> str:
        return ENV_PREFIX + self.key.upper().replace(".", "_")


SETTINGS: tuple[Setting, ...] = (
    Setting("log_level", "INFO", "Log level: `DEBUG`, `INFO`, `WARNING` or `ERROR`."),
    Setting("listen.host", "127.0.0.1", "Address the admin HTTP API binds to."),
    Setting("listen.port", 8080, "Port the admin HTTP API listens on."),
    Setting("state_dir", "/var/lib/mitm-proxy", "Directory for persistent state (the CA)."),
    Setting(
        "proxy.host",
        "127.0.0.1",
        "Address the proxy listens on. Anything but loopback without `proxy.auth` "
        "is an open proxy.",
    ),
    Setting("proxy.port", 3128, "Port the proxy listens on."),
    Setting(
        "proxy.auth",
        "",
        "`user:password` for Basic proxy authentication. Empty: no authentication.",
        secret=True,
    ),
    Setting(
        "proxy.peek_timeout_ms",
        1000,
        "How long to wait for a TLS ClientHello after CONNECT before relaying the tunnel raw.",
    ),
    Setting(
        "proxy.idle_timeout_ms",
        120000,
        "Close a client connection or tunnel after this long with no traffic.",
    ),
    Setting(
        "proxy.shutdown_grace_ms",
        10000,
        "On SIGTERM, how long in-flight connections get to finish.",
    ),
    Setting(
        "intercept.hosts",
        "",
        "The allowlist: hosts to intercept (`example.org`, `*.example.org`, IP literals). "
        "Everything else is tunnelled untouched. Empty: intercept nothing.",
        is_list=True,
    ),
    Setting(
        "middleware.available",
        "",
        "Installed middleware that may run, as `name=module:attribute`. "
        "The built-in `log` is always available.",
        is_list=True,
    ),
    Setting(
        "middleware.chain",
        "",
        "The middleware to run, by name, in order.",
        is_list=True,
    ),
    Setting(
        "middleware.hook_timeout_ms",
        5000,
        "A middleware hook that runs longer than this has failed, and is skipped for the flow.",
    ),
    Setting(
        "body.max_buffer_bytes",
        67108864,
        "The largest body buffered for middleware. A larger body skips middleware and is "
        "streamed through.",
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
    Setting("ca.leaf_cache_size", 1024, "Leaf certificates kept in memory."),
    Setting(
        "upstream.extra_ca_file",
        "",
        "PEM bundle of extra CAs trusted for origin servers, on top of the system store.",
    ),
    Setting(
        "upstream.connect_timeout_ms",
        10000,
        "Connecting to an origin (TCP and TLS), including any wait for a free pool slot.",
    ),
    Setting(
        "upstream.idle_timeout_ms",
        60000,
        "Close a pooled upstream connection after this long unused.",
    ),
    Setting(
        "upstream.max_connections_per_host",
        6,
        "The most upstream connections kept to one host, port and SNI.",
    ),
)


@dataclass(frozen=True)
class Config:
    log_level: str
    host: str
    port: int
    state_dir: Path
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 3128
    proxy_auth: str = field(default="", repr=False)
    peek_timeout_ms: int = 1000
    idle_timeout_ms: int = 120000
    shutdown_grace_ms: int = 10000
    intercept_hosts: tuple[str, ...] = ()
    # name -> "module:attribute", including the built-in "log".
    middleware_available: Mapping[str, str] = field(
        default_factory=lambda: dict(BUILTIN_MIDDLEWARE)
    )
    middleware_chain: tuple[str, ...] = ()
    hook_timeout_ms: int = 5000
    max_buffer_bytes: int = 67108864
    ca_cert: str = ""
    ca_key: str = field(default="", repr=False)
    ca_common_name: str = "mitm-proxy CA"
    leaf_cache_size: int = 1024
    upstream_extra_ca_file: str = ""
    upstream_connect_timeout_ms: int = 10000
    upstream_idle_timeout_ms: int = 60000
    upstream_max_connections_per_host: int = 6
    # Setting key -> the resolved value, for display (secrets included: mask them).
    values: Mapping[str, Value] = field(default_factory=dict, repr=False)
    # Setting key -> where its value came from ("default", "file", an env var name).
    sources: Mapping[str, str] = field(default_factory=dict)


def _lookup(data: Mapping[str, Any], dotted: str) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _coerce(setting: Setting, raw: Any) -> Value:
    """Give raw (a string from the environment, or a YAML value) the setting's type."""
    if setting.is_list:
        if isinstance(raw, list):
            items = [str(item).strip() for item in raw]
        else:
            items = [item.strip() for item in str(raw).split(",")]
        return tuple(item for item in items if item)
    try:
        if isinstance(setting.default, bool):
            if isinstance(raw, bool):
                return raw
            text = str(raw).strip().lower()
            if text in {"1", "true", "yes", "on"}:
                return True
            if text in {"0", "false", "no", "off"}:
                return False
            raise ValueError(raw)
        if isinstance(setting.default, int):
            if isinstance(raw, bool):
                raise ValueError(raw)
            return int(raw)
    except ValueError:
        kind = type(setting.default).__name__
        raise ConfigError(f"{setting.key}: expected {kind}, got {raw!r}") from None
    return str(raw)


def resolve(
    setting: Setting, data: Mapping[str, Any], environ: Mapping[str, str]
) -> tuple[Value, str]:
    """The setting's value and where it came from."""
    file_var = f"{setting.env}_FILE"
    if file_var in environ:
        path = Path(environ[file_var])
        try:
            return _coerce(setting, path.read_text(encoding="utf-8").strip()), file_var
        except OSError as e:
            raise ConfigError(f"cannot read {file_var} ({path}): {e.strerror}") from e
    if setting.env in environ:
        return _coerce(setting, environ[setting.env]), setting.env
    value = _lookup(data, setting.key)
    if value is not None:
        return _coerce(setting, value), "file"
    default = _coerce(setting, setting.default) if setting.is_list else setting.default
    return default, "default"


def read_file(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path}: the top level must be a mapping")
    return loaded


def _port(values: Mapping[str, Value], key: str) -> int:
    port = _int(values, key)
    if not 0 < port < 65536:
        raise ConfigError(f"{key}: {port} is not a TCP port")
    return port


def _int(values: Mapping[str, Value], key: str) -> int:
    value = values[key]
    if not isinstance(value, int) or isinstance(value, bool):  # pragma: no cover - coerced
        raise ConfigError(f"{key}: expected int")
    return value


def _positive(values: Mapping[str, Value], key: str) -> int:
    value = _int(values, key)
    if value <= 0:
        raise ConfigError(f"{key}: must be greater than 0, got {value}")
    return value


def _list(values: Mapping[str, Value], key: str) -> tuple[str, ...]:
    value = values[key]
    return value if isinstance(value, tuple) else ()


def _middleware(values: Mapping[str, Value]) -> tuple[dict[str, str], tuple[str, ...]]:
    available = dict(BUILTIN_MIDDLEWARE)
    for entry in _list(values, "middleware.available"):
        name, sep, ref = entry.partition("=")
        name, ref = name.strip(), ref.strip()
        if not sep or not _NAME.match(name) or not _REF.match(ref):
            raise ConfigError(f"middleware.available: {entry!r} is not name=module:attribute")
        if name in available:
            raise ConfigError(f"middleware.available: {name!r} is defined twice")
        available[name] = ref
    chain = _list(values, "middleware.chain")
    for name in chain:
        if name not in available:
            raise ConfigError(
                f"middleware.chain: {name!r} is not in middleware.available "
                f"(known: {', '.join(sorted(available))})"
            )
    if len(set(chain)) != len(chain):
        raise ConfigError("middleware.chain: a middleware is listed twice")
    return available, chain


def load_config(path: Path | None = None, environ: Mapping[str, str] | None = None) -> Config:
    """Load the configuration. A missing config file is not an error: defaults apply."""
    env = os.environ if environ is None else environ
    if path is None:
        path = Path(env.get(CONFIG_ENV, str(DEFAULT_CONFIG_PATH)))
    data = read_file(path)

    values: dict[str, Value] = {}
    sources: dict[str, str] = {}
    for setting in SETTINGS:
        values[setting.key], sources[setting.key] = resolve(setting, data, env)

    log_level = str(values["log_level"]).upper()
    if log_level not in LOG_LEVELS:
        raise ConfigError(f"log_level: expected one of {', '.join(LOG_LEVELS)}, got {log_level!r}")
    values["log_level"] = log_level

    hosts = _list(values, "intercept.hosts")
    for entry in hosts:
        try:
            parse_pattern(entry)
        except ValueError as e:
            raise ConfigError(f"intercept.hosts: {e}") from None

    available, chain = _middleware(values)

    proxy_auth = str(values["proxy.auth"])
    if proxy_auth:
        user, sep, _ = proxy_auth.partition(":")
        if not sep or not user:
            raise ConfigError("proxy.auth: expected user:password")

    ca_cert, ca_key = str(values["ca.cert"]), str(values["ca.key"])
    if bool(ca_cert) != bool(ca_key):
        raise ConfigError("ca.cert and ca.key: set both, or neither")

    for key in (
        "proxy.peek_timeout_ms",
        "proxy.idle_timeout_ms",
        "proxy.shutdown_grace_ms",
        "middleware.hook_timeout_ms",
        "body.max_buffer_bytes",
        "ca.leaf_cache_size",
        "upstream.connect_timeout_ms",
        "upstream.idle_timeout_ms",
        "upstream.max_connections_per_host",
    ):
        _positive(values, key)

    return Config(
        log_level=log_level,
        host=str(values["listen.host"]),
        port=_port(values, "listen.port"),
        state_dir=Path(str(values["state_dir"])),
        proxy_host=str(values["proxy.host"]),
        proxy_port=_port(values, "proxy.port"),
        proxy_auth=proxy_auth,
        peek_timeout_ms=_int(values, "proxy.peek_timeout_ms"),
        idle_timeout_ms=_int(values, "proxy.idle_timeout_ms"),
        shutdown_grace_ms=_int(values, "proxy.shutdown_grace_ms"),
        intercept_hosts=hosts,
        middleware_available=available,
        middleware_chain=chain,
        hook_timeout_ms=_int(values, "middleware.hook_timeout_ms"),
        max_buffer_bytes=_int(values, "body.max_buffer_bytes"),
        ca_cert=ca_cert,
        ca_key=ca_key,
        ca_common_name=str(values["ca.common_name"]),
        leaf_cache_size=_int(values, "ca.leaf_cache_size"),
        upstream_extra_ca_file=str(values["upstream.extra_ca_file"]),
        upstream_connect_timeout_ms=_int(values, "upstream.connect_timeout_ms"),
        upstream_idle_timeout_ms=_int(values, "upstream.idle_timeout_ms"),
        upstream_max_connections_per_host=_int(values, "upstream.max_connections_per_host"),
        values=values,
        sources=sources,
    )
