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


def test_serve_runs_the_service(
    monkeypatch: pytest.MonkeyPatch, write_config: Callable[..., Path]
) -> None:
    from mitm_proxy import service
    from mitm_proxy.config import Config

    seen: list[Config] = []

    async def fake_run(config: Config) -> None:
        seen.append(config)

    monkeypatch.setattr(service, "run", fake_run)
    result = runner.invoke(app, ["serve", "-c", str(write_config("proxy:\n  port: 9100\n"))])
    assert result.exit_code == 0
    assert seen[0].proxy_port == 9100


def test_serve_startup_error_exits_2(
    monkeypatch: pytest.MonkeyPatch, write_config: Callable[..., Path]
) -> None:
    from mitm_proxy import service
    from mitm_proxy.config import Config
    from mitm_proxy.errors import StartupError

    async def fail(config: Config) -> None:
        raise StartupError("cannot listen on 127.0.0.1:3128: Address already in use")

    monkeypatch.setattr(service, "run", fail)
    result = runner.invoke(app, ["serve", "-c", str(write_config(""))])
    assert result.exit_code == 2
    assert result.stderr.strip() == "error: cannot listen on 127.0.0.1:3128: Address already in use"


def test_ca_init_then_show(tmp_path: Path, write_config: Callable[..., Path]) -> None:
    path = write_config(f"state_dir: {tmp_path / 'state'}\n")
    created = runner.invoke(app, ["ca", "init", "-c", str(path)])
    assert created.exit_code == 0 and "SHA-256" in created.stdout
    again = runner.invoke(app, ["ca", "init", "-c", str(path)])
    assert again.exit_code == 2 and "--force" in again.stderr
    assert runner.invoke(app, ["ca", "init", "-c", str(path), "--force"]).exit_code == 0
    shown = runner.invoke(app, ["ca", "show", "-c", str(path)])
    assert shown.exit_code == 0
    assert shown.stdout.startswith("-----BEGIN CERTIFICATE-----")
    assert "SHA-256" in shown.stderr
    der = runner.invoke(app, ["ca", "show", "--der", "-c", str(path)])
    assert der.exit_code == 0 and der.stdout_bytes.startswith(b"0\x82")


def test_ca_show_without_a_ca(tmp_path: Path, write_config: Callable[..., Path]) -> None:
    result = runner.invoke(app, ["ca", "show", "-c", str(write_config(f"state_dir: {tmp_path}\n"))])
    assert result.exit_code == 2 and "ca init" in result.stderr


def test_ca_init_refuses_when_the_ca_comes_from_settings(
    monkeypatch: pytest.MonkeyPatch, write_config: Callable[..., Path]
) -> None:
    monkeypatch.setenv("MITM_PROXY_CA_CERT", "pem")
    monkeypatch.setenv("MITM_PROXY_CA_KEY", "pem")
    result = runner.invoke(app, ["ca", "init", "-c", str(write_config(""))])
    assert result.exit_code == 2 and "settings" in result.stderr


@pytest.mark.parametrize(
    ("host", "code", "text"),
    [
        ("www.example.org", 0, "intercepted (matches *.example.org)"),
        ("example.net", 1, "tunnelled untouched"),
        ("bad host", 2, "not a hostname"),
    ],
)
def test_scope_check(write_config: Callable[..., Path], host: str, code: int, text: str) -> None:
    path = write_config("intercept:\n  hosts: ['*.example.org']\n")
    result = runner.invoke(app, ["scope", "check", host, "-c", str(path)])
    assert result.exit_code == code
    assert text in (result.stdout + result.stderr)
