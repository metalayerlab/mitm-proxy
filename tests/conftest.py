"""Shared fixtures."""

from collections.abc import Callable
from pathlib import Path

import pytest

from mitm_proxy.config import ENV_PREFIX


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test sees the developer's own MITM_PROXY_* variables."""
    import os

    for name in list(os.environ):
        if name.startswith(ENV_PREFIX):
            monkeypatch.delenv(name)


@pytest.fixture
def write_config(tmp_path: Path) -> Callable[..., Path]:
    """Write a config file and return its path."""

    def write(text: str) -> Path:
        path = tmp_path / "config.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    return write


ASYNC_TEST_TIMEOUT = 10.0


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
    """Run `async def` tests on a fresh event loop, failing any that hang."""
    import asyncio
    import inspect

    if not inspect.iscoroutinefunction(pyfuncitem.obj):
        return None
    names = pyfuncitem._fixtureinfo.argnames
    kwargs = {name: pyfuncitem.funcargs[name] for name in names}
    asyncio.run(asyncio.wait_for(pyfuncitem.obj(**kwargs), ASYNC_TEST_TIMEOUT))
    return True
