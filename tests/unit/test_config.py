from collections.abc import Callable
from pathlib import Path

import pytest

from mitm_proxy.config import ConfigError, load_config


def test_defaults_when_no_file(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "missing.yaml", environ={})
    assert cfg.log_level == "INFO"
    assert cfg.sources["log_level"] == "default"


def test_file_overrides_default(write_config: Callable[..., Path]) -> None:
    cfg = load_config(write_config("log_level: debug\n"), environ={})
    assert cfg.log_level == "DEBUG"
    assert cfg.sources["log_level"] == "file"


def test_env_overrides_file(write_config: Callable[..., Path]) -> None:
    path = write_config("log_level: debug\n")
    cfg = load_config(path, environ={"MITM_PROXY_LOG_LEVEL": "warning"})
    assert cfg.log_level == "WARNING"
    assert cfg.sources["log_level"] == "MITM_PROXY_LOG_LEVEL"


def test_file_variable_overrides_env(tmp_path: Path) -> None:
    secret = tmp_path / "level"
    secret.write_text("error\n", encoding="utf-8")
    env = {"MITM_PROXY_LOG_LEVEL": "warning", "MITM_PROXY_LOG_LEVEL_FILE": str(secret)}
    cfg = load_config(tmp_path / "missing.yaml", environ=env)
    assert cfg.log_level == "ERROR"
    assert cfg.sources["log_level"] == "MITM_PROXY_LOG_LEVEL_FILE"


def test_config_path_from_environment(write_config: Callable[..., Path]) -> None:
    path = write_config("log_level: error\n")
    cfg = load_config(environ={"MITM_PROXY_CONFIG": str(path)})
    assert cfg.log_level == "ERROR"


def test_unreadable_file_variable(tmp_path: Path) -> None:
    env = {"MITM_PROXY_LOG_LEVEL_FILE": str(tmp_path / "nope")}
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "missing.yaml", environ=env)


def test_invalid_log_level(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="log_level"):
        load_config(tmp_path / "missing.yaml", environ={"MITM_PROXY_LOG_LEVEL": "loud"})


def test_top_level_must_be_mapping(write_config: Callable[..., Path]) -> None:
    with pytest.raises(ConfigError, match="mapping"):
        load_config(write_config("- a\n- b\n"), environ={})


def test_empty_file_is_defaults(write_config: Callable[..., Path]) -> None:
    assert load_config(write_config(""), environ={}).log_level == "INFO"


def test_invalid_yaml(write_config: Callable[..., Path]) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(write_config("log_level: [unclosed\n"), environ={})


def test_nested_key_and_int_coercion(write_config: Callable[..., Path]) -> None:
    cfg = load_config(write_config("listen:\n  port: 9000\n"), environ={})
    assert cfg.port == 9000
    cfg = load_config(write_config(""), environ={"MITM_PROXY_LISTEN_PORT": "9001"})
    assert cfg.port == 9001


def test_bad_port(write_config: Callable[..., Path]) -> None:
    with pytest.raises(ConfigError, match="expected int"):
        load_config(write_config(""), environ={"MITM_PROXY_LISTEN_PORT": "http"})
    with pytest.raises(ConfigError, match="not a TCP port"):
        load_config(write_config(""), environ={"MITM_PROXY_LISTEN_PORT": "70000"})


@pytest.mark.parametrize(
    ("raw", "expected"), [("yes", True), ("0", False), (True, True), ("off", False)]
)
def test_bool_coercion(raw: object, expected: bool) -> None:
    from mitm_proxy.config import Setting, _coerce

    assert _coerce(Setting("flag", False, "a flag"), raw) is expected


def test_bool_coercion_rejects_garbage() -> None:
    from mitm_proxy.config import Setting, _coerce

    with pytest.raises(ConfigError, match="expected bool"):
        _coerce(Setting("flag", False, "a flag"), "maybe")


def test_new_defaults(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "missing.yaml", environ={})
    assert cfg.host == "127.0.0.1"
    assert (cfg.proxy_host, cfg.proxy_port) == ("127.0.0.1", 3128)
    assert cfg.intercept_hosts == ()
    assert cfg.middleware_chain == ()
    assert cfg.middleware_available == {"log": "mitm_proxy.middleware.log:FlowLogger"}
    assert cfg.values["intercept.hosts"] == ()


def test_lists_from_yaml_and_env(write_config: Callable[..., Path]) -> None:
    path = write_config("intercept:\n  hosts: [example.org, '*.example.org']\n")
    assert load_config(path, environ={}).intercept_hosts == ("example.org", "*.example.org")
    env = {"MITM_PROXY_INTERCEPT_HOSTS": "example.org, *.example.net ,"}
    assert load_config(path, environ=env).intercept_hosts == ("example.org", "*.example.net")


def test_invalid_allowlist_entry(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"intercept\.hosts"):
        load_config(tmp_path / "x.yaml", environ={"MITM_PROXY_INTERCEPT_HOSTS": "*"})


def test_middleware_registry_and_chain(write_config: Callable[..., Path]) -> None:
    path = write_config(
        "middleware:\n  available: ['strip=pkg.mod:Strip']\n  chain: [strip, log]\n"
    )
    cfg = load_config(path, environ={})
    assert cfg.middleware_available["strip"] == "pkg.mod:Strip"
    assert cfg.middleware_chain == ("strip", "log")


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        ("middleware:\n  chain: [nope]\n", "not in middleware.available"),
        ("middleware:\n  chain: [log, log]\n", "listed twice"),
        ("middleware:\n  available: ['x=no-colon']\n", "name=module:attribute"),
        ("middleware:\n  available: ['log=a.b:C']\n", "defined twice"),
    ],
)
def test_middleware_errors(write_config: Callable[..., Path], yaml_text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(yaml_text), environ={})


@pytest.mark.parametrize("auth", ["nocolon", ":password"])
def test_proxy_auth_must_be_user_password(tmp_path: Path, auth: str) -> None:
    with pytest.raises(ConfigError, match=r"proxy\.auth"):
        load_config(tmp_path / "x.yaml", environ={"MITM_PROXY_PROXY_AUTH": auth})


def test_proxy_auth_from_file(tmp_path: Path) -> None:
    secret = tmp_path / "auth"
    secret.write_text("user:pass\n", encoding="utf-8")
    cfg = load_config(tmp_path / "x.yaml", environ={"MITM_PROXY_PROXY_AUTH_FILE": str(secret)})
    assert cfg.proxy_auth == "user:pass"
    assert "pass" not in repr(cfg)


def test_ca_cert_and_key_go_together(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="both"):
        load_config(tmp_path / "x.yaml", environ={"MITM_PROXY_CA_CERT": "pem"})


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"MITM_PROXY_PROXY_PORT": "0"}, "not a TCP port"),
        ({"MITM_PROXY_LISTEN_PORT": "70000"}, "not a TCP port"),
        ({"MITM_PROXY_BODY_MAX_BUFFER_BYTES": "0"}, "greater than 0"),
        ({"MITM_PROXY_PROXY_PEEK_TIMEOUT_MS": "soon"}, "expected int"),
    ],
)
def test_numeric_validation(tmp_path: Path, env: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(tmp_path / "x.yaml", environ=env)


def test_yaml_bool_is_not_an_int(write_config: Callable[..., Path]) -> None:
    with pytest.raises(ConfigError, match="expected int"):
        load_config(write_config("proxy:\n  port: true\n"), environ={})


def test_example_config_is_the_defaults() -> None:
    example = Path(__file__).parents[2] / "contrib" / "mitm-proxy.example.yaml"
    cfg = load_config(example, environ={})
    defaults = load_config(example.with_name("missing.yaml"), environ={})
    assert cfg.values == defaults.values
