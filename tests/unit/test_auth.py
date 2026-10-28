import base64

import pytest

from mitm_proxy.auth import proxy_authorized


def basic(text: str) -> str:
    return "Basic " + base64.b64encode(text.encode()).decode()


@pytest.mark.parametrize(
    ("header", "ok"),
    [
        (basic("user:pass"), True),
        (basic("user:wrong"), False),
        ("Bearer abc", False),
        ("Basic !!!notbase64", False),
        (None, False),
        ("", False),
    ],
)
def test_proxy_authorized(header: str | None, ok: bool) -> None:
    assert proxy_authorized("user:pass", header) is ok


def test_no_credentials_configured_means_open() -> None:
    assert proxy_authorized("", None)
