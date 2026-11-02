"""mitm-proxy's settings and rules on top of interpose (design 0008).

interpose's own tests cover the engine (lookup order, coercion, `_FILE` reading, YAML errors,
`Secret`). These cover what is mitm-proxy's: the names, the wiring, the checks and the rules
that span settings, and that the secrets stay secret here.
"""

from collections.abc import Callable
from pathlib import Path

import pytest
from interpose.secret import MASK, Secret

from mitm_proxy.config import (
    CONFIG_ENV,
    DEFAULT_CONFIG_PATH,
    SETTINGS,
    ConfigError,
    load_config,
)

# Every key and environment variable as they were before interpose: a rename breaks deployments.
NAMES = {
    "log_level": "MITM_PROXY_LOG_LEVEL",
    "listen.host": "MITM_PROXY_LISTEN_HOST",
    "listen.port": "MITM_PROXY_LISTEN_PORT",
    "state_dir": "MITM_PROXY_STATE_DIR",
    "proxy.host": "MITM_PROXY_PROXY_HOST",
    "proxy.port": "MITM_PROXY_PROXY_PORT",
    "proxy.auth": "MITM_PROXY_PROXY_AUTH",
    "proxy.peek_timeout_ms": "MITM_PROXY_PROXY_PEEK_TIMEOUT_MS",
    "proxy.idle_timeout_ms": "MITM_PROXY_PROXY_IDLE_TIMEOUT_MS",
    "proxy.shutdown_grace_ms": "MITM_PROXY_PROXY_SHUTDOWN_GRACE_MS",
    "proxy.access_log": "MITM_PROXY_PROXY_ACCESS_LOG",
    "proxy.access_log_relayed": "MITM_PROXY_PROXY_ACCESS_LOG_RELAYED",
    "proxy.server_timing": "MITM_PROXY_PROXY_SERVER_TIMING",
    "intercept.hosts": "MITM_PROXY_INTERCEPT_HOSTS",
    "middleware.available": "MITM_PROXY_MIDDLEWARE_AVAILABLE",
    "middleware.chain": "MITM_PROXY_MIDDLEWARE_CHAIN",
    "middleware.hook_timeout_ms": "MITM_PROXY_MIDDLEWARE_HOOK_TIMEOUT_MS",
    "body.max_buffer_bytes": "MITM_PROXY_BODY_MAX_BUFFER_BYTES",
    "ca.cert": "MITM_PROXY_CA_CERT",
    "ca.key": "MITM_PROXY_CA_KEY",
    "ca.common_name": "MITM_PROXY_CA_COMMON_NAME",
    "ca.leaf_cache_size": "MITM_PROXY_CA_LEAF_CACHE_SIZE",
    "upstream.extra_ca_file": "MITM_PROXY_UPSTREAM_EXTRA_CA_FILE",
    "upstream.connect_timeout_ms": "MITM_PROXY_UPSTREAM_CONNECT_TIMEOUT_MS",
    "upstream.idle_timeout_ms": "MITM_PROXY_UPSTREAM_IDLE_TIMEOUT_MS",
    "upstream.max_connections_per_host": "MITM_PROXY_UPSTREAM_MAX_CONNECTIONS_PER_HOST",
}


def test_keys_and_environment_variables_are_unchanged() -> None:
    assert {s.key: s.env for s in SETTINGS} == NAMES
    assert list(NAMES) == [s.key for s in SETTINGS]
    assert CONFIG_ENV == "MITM_PROXY_CONFIG"
    assert Path("/etc/mitm-proxy/config.yaml") == DEFAULT_CONFIG_PATH


def test_only_proxy_auth_and_ca_key_are_secret() -> None:
    assert {s.key for s in SETTINGS if s.secret} == {"proxy.auth", "ca.key"}


def test_defaults_when_no_file(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "missing.yaml", environ={})
    assert cfg.log_level == "INFO"
    assert cfg.sources["log_level"] == "default"
    assert cfg.host == "127.0.0.1"
    assert (cfg.proxy_host, cfg.proxy_port) == ("127.0.0.1", 3128)
    assert cfg.intercept_hosts == ()
    assert cfg.middleware_chain == ()
    assert cfg.middleware_available == {}
    assert (cfg.access_log, cfg.access_log_relayed) == (False, False)
    assert cfg.values["intercept.hosts"] == ()
    assert cfg.proxy_auth == Secret("") and not cfg.proxy_auth
    assert cfg.ca_key == Secret("") and not cfg.ca_key


def test_lookup_order_with_mitm_proxy_names(
    tmp_path: Path, write_config: Callable[..., Path]
) -> None:
    path = write_config("log_level: debug\n")
    assert load_config(path, environ={}).sources["log_level"] == "file"
    env = {"MITM_PROXY_LOG_LEVEL": "warning"}
    assert load_config(path, environ=env).sources["log_level"] == "MITM_PROXY_LOG_LEVEL"
    level = tmp_path / "level"
    level.write_text("error\n", encoding="utf-8")
    cfg = load_config(path, environ={**env, "MITM_PROXY_LOG_LEVEL_FILE": str(level)})
    assert cfg.log_level == "ERROR"
    assert cfg.sources["log_level"] == "MITM_PROXY_LOG_LEVEL_FILE"


def test_config_path_from_environment(write_config: Callable[..., Path]) -> None:
    path = write_config("log_level: error\n")
    cfg = load_config(environ={"MITM_PROXY_CONFIG": str(path)})
    assert cfg.log_level == "ERROR"


@pytest.mark.parametrize("given", ["debug", "Debug", "DEBUG"])
def test_log_level_is_upper_case_whatever_it_was_given_as(
    write_config: Callable[..., Path], given: str
) -> None:
    cfg = load_config(write_config(f"log_level: {given}\n"), environ={})
    assert cfg.log_level == "DEBUG"
    assert cfg.values["log_level"] == "DEBUG"


def test_invalid_log_level(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="log_level: expected one of DEBUG, INFO"):
        load_config(tmp_path / "missing.yaml", environ={"MITM_PROXY_LOG_LEVEL": "loud"})


def test_lists_reach_the_config(write_config: Callable[..., Path]) -> None:
    path = write_config("intercept:\n  hosts: [example.org, '*.example.org']\n")
    assert load_config(path, environ={}).intercept_hosts == ("example.org", "*.example.org")


def test_invalid_allowlist_entry(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"intercept\.hosts: .*every host"):
        load_config(tmp_path / "x.yaml", environ={"MITM_PROXY_INTERCEPT_HOSTS": "*"})


def test_middleware_registry_and_chain(write_config: Callable[..., Path]) -> None:
    path = write_config("middleware:\n  available: ['strip=pkg.mod:Strip']\n  chain: [strip]\n")
    cfg = load_config(path, environ={})
    assert cfg.middleware_available["strip"] == "pkg.mod:Strip"
    assert cfg.middleware_chain == ("strip",)


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        ("middleware:\n  chain: [nope]\n", "not in middleware.available"),
        ("middleware:\n  available: ['a=m:A']\n  chain: [a, a]\n", "chain: a middleware is listed"),
        ("middleware:\n  chain: [log]\n", "replaced by the proxy.access_log setting"),
        ("middleware:\n  available: ['x=no-colon']\n", "available: 'x=no-colon' is not name="),
        ("middleware:\n  available: ['x=a.b:C', 'x=d.e:F']\n", "available: 'x' is defined twice"),
    ],
)
def test_middleware_errors(write_config: Callable[..., Path], yaml_text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(yaml_text), environ={})


@pytest.mark.parametrize("auth", ["nocolon-s3cret", ":s3cret"])
def test_proxy_auth_must_be_user_password_and_the_error_hides_it(tmp_path: Path, auth: str) -> None:
    with pytest.raises(ConfigError, match=r"proxy\.auth: expected user:password") as e:
        load_config(tmp_path / "x.yaml", environ={"MITM_PROXY_PROXY_AUTH": auth})
    assert "s3cret" not in str(e.value)


def test_proxy_auth_is_a_secret(tmp_path: Path) -> None:
    secret = tmp_path / "auth"
    secret.write_text("user:s3cret\n", encoding="utf-8")
    cfg = load_config(tmp_path / "x.yaml", environ={"MITM_PROXY_PROXY_AUTH_FILE": str(secret)})
    assert isinstance(cfg.proxy_auth, Secret)
    assert cfg.proxy_auth.reveal() == "user:s3cret"
    assert str(cfg.proxy_auth) == MASK and f"{cfg.proxy_auth}" == MASK
    assert "s3cret" not in repr(cfg)
    assert "s3cret" not in repr(cfg.values)


def test_ca_key_is_a_secret(tmp_path: Path) -> None:
    env = {"MITM_PROXY_CA_CERT": "cert-pem", "MITM_PROXY_CA_KEY": "key-s3cret"}
    cfg = load_config(tmp_path / "x.yaml", environ=env)
    assert isinstance(cfg.ca_key, Secret)
    assert cfg.ca_key.reveal() == "key-s3cret"
    assert "s3cret" not in repr(cfg)


def test_ca_cert_and_key_go_together(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="both"):
        load_config(tmp_path / "x.yaml", environ={"MITM_PROXY_CA_CERT": "pem"})
    with pytest.raises(ConfigError, match="both") as e:
        load_config(tmp_path / "x.yaml", environ={"MITM_PROXY_CA_KEY": "key-s3cret"})
    assert "s3cret" not in str(e.value)


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"MITM_PROXY_PROXY_PORT": "0"}, "proxy.port: 0 is not a TCP port"),
        ({"MITM_PROXY_LISTEN_PORT": "70000"}, "listen.port: 70000 is not a TCP port"),
        ({"MITM_PROXY_LISTEN_PORT": "http"}, "listen.port: expected int"),
        ({"MITM_PROXY_BODY_MAX_BUFFER_BYTES": "0"}, "max_buffer_bytes: must be greater than 0"),
        ({"MITM_PROXY_CA_LEAF_CACHE_SIZE": "-1"}, "leaf_cache_size: must be greater than 0"),
        ({"MITM_PROXY_PROXY_PEEK_TIMEOUT_MS": "soon"}, "peek_timeout_ms: expected int"),
    ],
)
def test_numeric_validation(tmp_path: Path, env: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(tmp_path / "x.yaml", environ=env)


# Stricter than before interpose, on purpose (design 0008): each of these used to be accepted.


@pytest.mark.parametrize(
    "yaml_text",
    [
        "proxy:\n  port: 3128.0\n",  # was 3128
        "body:\n  max_buffer_bytes: 0.5\n",  # was 0, then "must be greater than 0"
        "upstream:\n  idle_timeout_ms: 1500.0\n",  # was 1500
    ],
)
def test_a_float_for_an_int_setting_is_an_error(
    write_config: Callable[..., Path], yaml_text: str
) -> None:
    with pytest.raises(ConfigError, match="expected int"):
        load_config(write_config(yaml_text), environ={})


@pytest.mark.parametrize(
    "yaml_text",
    [
        "listen:\n  host: [127.0.0.1, '::1']\n",  # was the string "['127.0.0.1', '::1']"
        "ca:\n  common_name: {name: lab}\n",  # was "{'name': 'lab'}"
    ],
)
def test_a_list_or_mapping_for_a_str_setting_is_an_error(
    write_config: Callable[..., Path], yaml_text: str
) -> None:
    with pytest.raises(ConfigError, match="expected str"):
        load_config(write_config(yaml_text), environ={})


def test_an_underscored_int_from_the_environment_is_an_error(tmp_path: Path) -> None:
    env = {"MITM_PROXY_BODY_MAX_BUFFER_BYTES": "1_000"}
    with pytest.raises(ConfigError, match="expected int"):
        load_config(tmp_path / "x.yaml", environ=env)


def test_a_secret_settings_type_error_does_not_echo_it(write_config: Callable[..., Path]) -> None:
    with pytest.raises(ConfigError, match=r"proxy\.auth: expected str") as e:
        load_config(write_config("proxy:\n  auth: [user, s3cret]\n"), environ={})
    assert "s3cret" not in str(e.value)


def test_example_config_is_the_defaults() -> None:
    example = Path(__file__).parents[2] / "contrib" / "mitm-proxy.example.yaml"
    cfg = load_config(example, environ={})
    defaults = load_config(example.with_name("missing.yaml"), environ={})
    assert cfg.values == defaults.values


def test_server_timing_is_off_by_default_and_set_from_file_or_env(
    write_config: Callable[..., Path],
) -> None:
    path = write_config("proxy:\n  server_timing: true\n")
    assert load_config(path.parent / "missing.yaml", environ={}).server_timing is False
    assert load_config(path, environ={}).server_timing is True
    env = {"MITM_PROXY_PROXY_SERVER_TIMING": "false"}
    assert load_config(path, environ=env).server_timing is False


def test_access_log_settings(write_config: Callable[..., Path]) -> None:
    path = write_config("proxy:\n  access_log: true\n")
    cfg = load_config(path, environ={"MITM_PROXY_PROXY_ACCESS_LOG_RELAYED": "true"})
    assert (cfg.access_log, cfg.access_log_relayed) == (True, True)
