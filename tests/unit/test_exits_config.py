"""The `exits` and `routing.*` settings (design 0013, sections 2 and 3; step 1)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from interpose.secret import Secret

from mitm_proxy.config import ConfigError, Exit, load_config
from mitm_proxy.routing import DIRECT_ROUTE, Router

EXAMPLE = """\
exits:
  - name: exit-a
    url: http://exit-a.exits:8001
    country: XA
  - name: exit-b
    url: http://exit-b.exits:8002
    country: XB
  - name: exit-tor
    url: socks5h://exit-tor.exits:9050
    retry: false
routing:
  default_via: [exit-b]
"""


def env(**pairs: str) -> dict[str, str]:
    return {f"MITM_PROXY_{key}": value for key, value in pairs.items()}


def load(environ: dict[str, str]) -> object:
    return load_config(Path("/nonexistent/config.yaml"), environ=environ)


def test_the_records_example_loads(write_config: Callable[[str], Path]) -> None:
    config = load_config(write_config(EXAMPLE), environ={})
    assert config.exits == (
        Exit("exit-a", "http", "exit-a.exits", 8001, country="XA"),
        Exit("exit-b", "http", "exit-b.exits", 8002, country="XB"),
        Exit("exit-tor", "socks5h", "exit-tor.exits", 9050, retry=False),
    )
    assert config.routing_default_via == ("exit-b",)
    assert config.routing_allow_direct is False
    assert [e.url for e in config.exits] == [
        "http://exit-a.exits:8001",
        "http://exit-b.exits:8002",
        "socks5h://exit-tor.exits:9050",
    ]
    # health: the Service named by the host part up to the first dot, until set.
    assert [e.health_service for e in config.exits] == ["exit-a", "exit-b", "exit-tor"]


def test_exits_default_to_empty_and_routing_off() -> None:
    config = load_config(Path("/nonexistent/config.yaml"), environ={})
    assert config.exits == ()
    assert config.routing_default_via == ()
    assert config.routing_allow_direct is False
    assert Router(config.exits, config.routing_default_via).route("example.org") is DIRECT_ROUTE


def test_exits_from_indexed_variables_with_the_login_from_a_file(tmp_path: Path) -> None:
    login = tmp_path / "exit-a-auth"
    login.write_text("tester:made-up-for-a-test\n")
    config = load_config(
        tmp_path / "none.yaml",
        environ=env(
            EXITS_0_NAME="exit-a",
            EXITS_0_URL="http://exit-a.exits:8001",
            EXITS_0_AUTH_FILE=str(login),
            EXITS_0_HEALTH="exit-a-svc",
            ROUTING_DEFAULT_VIA="exit-a",
        ),
    )
    (exit_a,) = config.exits
    assert exit_a.auth == Secret("tester:made-up-for-a-test")
    assert exit_a.credentials() == ("tester", "made-up-for-a-test")
    assert exit_a.health_service == "exit-a-svc"
    assert "made-up" not in repr(exit_a) and "made-up" not in repr(config)


def test_a_login_as_a_secret_file_leaf(tmp_path: Path, write_config: Callable[[str], Path]) -> None:
    (tmp_path / "login").write_text("tester:made-up-for-a-test")
    path = write_config(
        "exits:\n  - name: exit-a\n    url: http://exit-a.exits:8001\n"
        "    auth: {secret_file: login}\n"
    )
    (exit_a,) = load_config(path, environ={}).exits
    assert exit_a.credentials() == ("tester", "made-up-for-a-test")


@pytest.mark.parametrize(
    ("pairs", "message"),
    [
        ({"EXITS_0_NAME": "Exit-A"}, "lower-case letters, digits and '-'"),
        ({"EXITS_0_NAME": "exit_a"}, "lower-case letters, digits and '-'"),
        ({"EXITS_0_NAME": "direct"}, "'direct' is reserved"),
        ({"EXITS_0_URL": "https://exit-a.exits:8001"}, "http://host:port or socks5h://host:port"),
        ({"EXITS_0_URL": "socks5://exit-a.exits:1080"}, "use socks5h://"),
        ({"EXITS_0_URL": "http://exit-a.exits:8001/"}, "no path"),
        ({"EXITS_0_URL": "http://exit-a.exits:8001/x"}, "no path"),
        ({"EXITS_0_URL": "http://exit-a.exits:8001?x=1"}, "no path or query"),
        ({"EXITS_0_URL": "http://exit-a.exits"}, "needs a port"),
        ({"EXITS_0_URL": "http://exit-a.exits:0"}, "needs a port"),
        ({"EXITS_0_URL": "http://exit-a.exits:99999"}, "exits\\[0\\].url"),
        ({"EXITS_0_URL": "http://:8001"}, "no host"),
        ({"EXITS_0_URL": "http://bad_label-.exits:8001"}, "not a host name"),
        ({"EXITS_0_HEALTH": "Not_A_Service"}, "not a Kubernetes Service name"),
        ({"EXITS_0_AUTH": "no-colon"}, "expected user:password"),
    ],
)
def test_an_invalid_exit_is_a_config_error(pairs: dict[str, str], message: str) -> None:
    base = {"EXITS_0_NAME": "exit-a", "EXITS_0_URL": "http://exit-a.exits:8001"}
    with pytest.raises(ConfigError, match=message):
        load(env(**{**base, **pairs}))


def test_a_password_in_the_url_is_a_config_error_that_never_shows_it() -> None:
    for url in (
        "http://tester:made-up-pw@exit-a.exits:8001",
        "socks5h://tester:made-up-pw@exit-a.exits:9050/path",
        "ftp://tester:made-up-pw@exit-a.exits:21",
        "http://tester@exit-a.exits:8001",
    ):
        with pytest.raises(ConfigError, match="no user info") as caught:
            load(env(EXITS_0_NAME="exit-a", EXITS_0_URL=url))
        assert "made-up-pw" not in str(caught.value)


def test_exit_names_are_unique(write_config: Callable[[str], Path]) -> None:
    text = (
        "exits:\n"
        "  - {name: exit-a, url: 'http://exit-a.exits:8001'}\n"
        "  - {name: exit-a, url: 'http://exit-b.exits:8002'}\n"
    )
    with pytest.raises(ConfigError, match=r"exits\[1\].name: 'exit-a' is already an exit"):
        load_config(write_config(text), environ={})


def test_a_socks5_login_must_fit_rfc_1929() -> None:
    with pytest.raises(ConfigError, match="255 bytes at most") as caught:
        load(
            env(
                EXITS_0_NAME="exit-tor",
                EXITS_0_URL="socks5h://exit-tor.exits:9050",
                EXITS_0_AUTH="tester:" + "x" * 256,
            )
        )
    assert "xxxx" not in str(caught.value)


def test_an_ipv6_exit() -> None:
    config = load_config(
        Path("/nonexistent"), environ=env(EXITS_0_NAME="exit-a", EXITS_0_URL="http://[::1]:8001")
    )
    assert (config.exits[0].host, config.exits[0].url) == ("::1", "http://[::1]:8001")


# routing.default_via and routing.allow_direct


def two_exits(**pairs: str) -> dict[str, str]:
    return env(
        EXITS_0_NAME="exit-a",
        EXITS_0_URL="http://exit-a.exits:8001",
        EXITS_1_NAME="exit-tor",
        EXITS_1_URL="socks5h://exit-tor.exits:9050",
        **pairs,
    )


def test_default_via_names_exits_in_order() -> None:
    config = load_config(
        Path("/nonexistent"), environ=two_exits(ROUTING_DEFAULT_VIA="exit-tor,exit-a")
    )
    assert config.routing_default_via == ("exit-tor", "exit-a")
    assert Router(config.exits, config.routing_default_via).route("x.test").exit == config.exits[1]


def test_default_via_must_name_an_exit() -> None:
    with pytest.raises(ConfigError, match=r"'exit-z' is not an exit \(exits: exit-a, exit-tor\)"):
        load(two_exits(ROUTING_DEFAULT_VIA="exit-a,exit-z"))


def test_default_via_without_exits_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="exits is empty"):
        load(env(ROUTING_DEFAULT_VIA="exit-a"))


def test_default_via_lists_an_exit_once() -> None:
    with pytest.raises(ConfigError, match="an exit is listed twice"):
        load(two_exits(ROUTING_DEFAULT_VIA="exit-a,exit-a"))


def test_direct_needs_allow_direct() -> None:
    with pytest.raises(ConfigError, match=r"only with routing\.allow_direct"):
        load(two_exits(ROUTING_DEFAULT_VIA="direct"))
    config = load_config(
        Path("/nonexistent"),
        environ=two_exits(ROUTING_DEFAULT_VIA="direct,exit-a", ROUTING_ALLOW_DIRECT="true"),
    )
    assert config.routing_allow_direct is True
    assert Router(config.exits, config.routing_default_via).route("x.test") is DIRECT_ROUTE


def test_allow_direct_alone_routes_nothing_directly() -> None:
    config = load_config(Path("/nonexistent"), environ=two_exits(ROUTING_ALLOW_DIRECT="true"))
    route = Router(config.exits, config.routing_default_via).route("x.test")
    assert (route.exit, route.refused) == (None, "no_rule")
