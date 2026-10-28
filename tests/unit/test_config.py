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
