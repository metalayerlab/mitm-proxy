from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mitm_proxy import __version__
from mitm_proxy.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_config_shows_sources(write_config: Callable[..., Path]) -> None:
    result = runner.invoke(app, ["config", "-c", str(write_config("log_level: debug\n"))])
    assert result.exit_code == 0
    assert "log_level = DEBUG  (file)" in result.stdout


def test_config_error_exits_2(write_config: Callable[..., Path]) -> None:
    result = runner.invoke(app, ["config", "-c", str(write_config("log_level: loud\n"))])
    assert result.exit_code == 2
    assert "log_level" in result.stderr


def test_secrets_are_masked(
    monkeypatch: pytest.MonkeyPatch, write_config: Callable[..., Path]
) -> None:
    from mitm_proxy import cli
    from mitm_proxy.config import SETTINGS, Setting

    secret = Setting(SETTINGS[0].key, SETTINGS[0].default, SETTINGS[0].help, secret=True)
    monkeypatch.setattr(cli, "SETTINGS", (secret, *SETTINGS[1:]))
    result = runner.invoke(app, ["config", "-c", str(write_config(""))])
    assert "log_level = ********" in result.stdout


def test_main_runs_app(monkeypatch: pytest.MonkeyPatch) -> None:
    from mitm_proxy import cli

    called: list[bool] = []
    monkeypatch.setattr(cli, "app", lambda: called.append(True))
    cli.main()
    assert called == [True]


def test_serve_starts_uvicorn(
    monkeypatch: pytest.MonkeyPatch, write_config: Callable[..., Path]
) -> None:
    import uvicorn

    seen: dict[str, object] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    result = runner.invoke(app, ["serve", "-c", str(write_config("listen:\n  port: 9100\n"))])
    assert result.exit_code == 0
    assert seen["port"] == 9100


def test_config_masks_set_secrets_and_joins_lists(
    monkeypatch: pytest.MonkeyPatch, write_config: Callable[..., Path]
) -> None:
    monkeypatch.setenv("MITM_PROXY_PROXY_AUTH", "user:secret")
    path = write_config("intercept:\n  hosts: [example.org, '*.example.org']\n")
    result = runner.invoke(app, ["config", "-c", str(path)])
    assert result.exit_code == 0
    assert "proxy.auth = ********  (MITM_PROXY_PROXY_AUTH)" in result.stdout
    assert "secret" not in result.stdout
    assert "intercept.hosts = example.org, *.example.org  (file)" in result.stdout
    assert "ca.key =   (default)" in result.stdout
