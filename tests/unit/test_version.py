import re

import mitm_proxy


def test_version_is_pep440() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+.*|0\+unknown", mitm_proxy.__version__)
