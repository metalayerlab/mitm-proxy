import pytest

from mitm_proxy.scope import Allowlist, normalize_host, parse_pattern


def test_exact_match_ignores_case_and_trailing_dot() -> None:
    allow = Allowlist(["example.org"])
    assert allow.match("EXAMPLE.org.") == "example.org"
    assert "www.example.org" not in allow


def test_wildcard_matches_any_depth_but_not_apex() -> None:
    allow = Allowlist(["*.example.org"])
    assert "a.example.org" in allow
    assert "a.b.example.org" in allow
    assert "example.org" not in allow
    assert "badexample.org" not in allow


def test_ip_literals_match_exactly_in_normal_form() -> None:
    allow = Allowlist(["2001:db8::1", "192.0.2.10"])
    assert "[2001:0db8:0:0::1]" in allow
    assert "192.0.2.10" in allow
    assert "192.0.2.11" not in allow


def test_idna_names_are_encoded() -> None:
    assert normalize_host("bücher.example.org") == "xn--bcher-kva.example.org"
    assert "bücher.example.org" in Allowlist(["xn--bcher-kva.example.org"])


def test_unparseable_host_never_matches() -> None:
    assert Allowlist(["*.example.org"]).match("bad host.example.org") is None


@pytest.mark.parametrize(
    "entry",
    [
        "*",
        "*.",
        "a.*.example.org",
        "*.a.*.org",
        "ex*ample.org",
        "*.192.0.2.1",
        "-bad.example.org",
        "",
    ],
)
def test_invalid_patterns_are_rejected(entry: str) -> None:
    with pytest.raises(ValueError):
        parse_pattern(entry)


def test_overlong_label_is_rejected() -> None:
    with pytest.raises(ValueError):
        normalize_host("a" * 64 + ".example.org")
