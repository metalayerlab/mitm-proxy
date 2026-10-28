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
from mitm_proxy.errors import CAError, StartupError

app = typer.Typer(
    help=DESCRIPTION,
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
ca_app = typer.Typer(
    help="The root CA that signs intercepted hosts' certificates.", no_args_is_help=True
)
scope_app = typer.Typer(help="The intercept allowlist.", no_args_is_help=True)
app.add_typer(ca_app, name="ca")
app.add_typer(scope_app, name="scope")

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


def _fail(message: object) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(2)


def _load(path: Path | None) -> Config:
    try:
        return load_config(path)
    except ConfigError as e:
        raise _fail(e) from e


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command("config")
def show_config(config: ConfigOption = None) -> None:
    """Print the effective configuration and where each value came from. Secrets are masked."""
    cfg = _load(config)
    for setting in SETTINGS:
        value = cfg.values.get(setting.key)
        if setting.secret:
            shown = "********" if value else ""
        elif isinstance(value, tuple):
            shown = ", ".join(value)
        else:
            shown = str(value)
        typer.echo(f"{setting.key} = {shown}  ({cfg.sources[setting.key]})")


@app.command()
def serve(config: ConfigOption = None) -> None:
    """Run the proxy and the admin HTTP API until SIGTERM or Ctrl-C.

    Loads the root CA (generating it on first start) and imports the middleware chain.
    """
    import asyncio
    import logging

    from mitm_proxy.service import run

    cfg = _load(config)
    logging.basicConfig(
        level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        asyncio.run(run(cfg))
    except (CAError, StartupError) as e:
        raise _fail(e) from e
    except KeyboardInterrupt:  # pragma: no cover - a second Ctrl-C during shutdown
        pass


@ca_app.command("init")
def ca_init(
    config: ConfigOption = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Replace an existing CA. Clients must trust the new one."),
    ] = False,
) -> None:
    """Create the root CA in <state_dir>/ca/ and print its SHA-256 fingerprint."""
    from mitm_proxy.tls.ca import fingerprint, generate_root, write_root

    cfg = _load(config)
    if cfg.ca_cert:
        raise _fail("ca.cert and ca.key are set: the CA comes from the settings, not state_dir")
    cert, key = generate_root(cfg.ca_common_name)
    try:
        write_root(cfg.state_dir / "ca", cert, key, force=force)
    except CAError as e:
        raise _fail(e) from e
    typer.echo(f"created {cfg.state_dir / 'ca'}; SHA-256 {fingerprint(cert)}")


@ca_app.command("show")
def ca_show(
    config: ConfigOption = None,
    der: Annotated[bool, typer.Option("--der", help="DER instead of PEM.")] = False,
) -> None:
    """Write the root CA certificate to stdout, to install on clients."""
    from cryptography.hazmat.primitives import serialization

    from mitm_proxy.tls.ca import fingerprint, load_root

    cfg = _load(config)
    try:
        cert, _ = load_root(
            cert_pem=cfg.ca_cert,
            key_pem=cfg.ca_key,
            state_dir=cfg.state_dir,
            common_name=cfg.ca_common_name,
            generate=False,
        )
    except CAError as e:
        raise _fail(e) from e
    encoding = serialization.Encoding.DER if der else serialization.Encoding.PEM
    typer.echo(cert.public_bytes(encoding), nl=False)
    typer.echo(f"SHA-256 {fingerprint(cert)}", err=True)


@scope_app.command("check")
def scope_check(
    host: Annotated[str, typer.Argument(help="A hostname or IP address.")],
    config: ConfigOption = None,
) -> None:
    """Say whether HOST would be intercepted, and which intercept.hosts entry matched.

    Exit code 0: intercepted. 1: tunnelled untouched. 2: an error.
    """
    from mitm_proxy.scope import Allowlist, normalize_host

    cfg = _load(config)
    try:
        canonical = normalize_host(host)
    except ValueError as e:
        raise _fail(e) from e
    entry = Allowlist(cfg.intercept_hosts).match(canonical)
    if entry is None:
        typer.echo(f"{canonical}: tunnelled untouched (not in intercept.hosts)")
        raise typer.Exit(1)
    typer.echo(f"{canonical}: intercepted (matches {entry})")


def main() -> None:
    app()
