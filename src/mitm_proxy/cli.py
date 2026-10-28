"""The `mitm-proxy` command.

The wiki's CLI-Reference page is generated from this module (scripts/gen-wiki.py),
so command docstrings and option help are user documentation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from mitm_proxy import DESCRIPTION, __version__
from mitm_proxy.config import CONFIG_ENV, SETTINGS, Config, ConfigError, load_config

app = typer.Typer(
    help=DESCRIPTION,
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)

ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config",
        "-c",
        envvar=CONFIG_ENV,
        help="Config file (default /etc/mitm-proxy/config.yaml; missing is fine).",
        show_default=False,
    ),
]


def _load(path: Path | None) -> Config:
    try:
        return load_config(path)
    except ConfigError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2) from e


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command("config")
def show_config(config: ConfigOption = None) -> None:
    """Print the effective configuration and where each value came from. Secrets are masked."""
    cfg = _load(config)
    values = {
        "log_level": cfg.log_level,
        "listen.host": cfg.host,
        "listen.port": cfg.port,
        "state_dir": cfg.state_dir,
    }
    for setting in SETTINGS:
        shown = "********" if setting.secret else values.get(setting.key)
        typer.echo(f"{setting.key} = {shown}  ({cfg.sources[setting.key]})")


@app.command()
def serve(config: ConfigOption = None) -> None:
    """Run the HTTP server."""
    import logging

    import uvicorn

    from mitm_proxy.api import create_app

    cfg = _load(config)
    logging.basicConfig(
        level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    uvicorn.run(
        create_app(cfg),
        host=cfg.host,
        port=cfg.port,
        log_level=cfg.log_level.lower(),
        proxy_headers=True,
    )


def main() -> None:
    app()
