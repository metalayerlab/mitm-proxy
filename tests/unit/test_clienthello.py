import pytest

from mitm_proxy.tls.clienthello import NotTLS, parse_client_hello
from tests.helpers import client_hello_bytes


def test_parses_sni_and_alpn_from_a_real_client_hello() -> None:
    hello = parse_client_hello(client_hello_bytes("example.org", ["h2", "http/1.1"]))
    assert hello is not None
    assert hello.sni == "example.org"
    assert hello.alpn == ("h2", "http/1.1")


def test_no_sni_and_no_alpn() -> None:
    hello = parse_client_hello(client_hello_bytes(None))
    assert hello is not None
    assert (hello.sni, hello.alpn) == (None, ())


def test_needs_more_data_until_the_message_is_complete() -> None:
    data = client_hello_bytes("example.org")
    for cut in (0, 3, 5, len(data) - 1):
        assert parse_client_hello(data[:cut]) is None
    assert parse_client_hello(data) is not None


def test_reassembles_a_hello_split_across_records() -> None:
    data = client_hello_bytes("example.org", ["h2"])
    body = data[5:]
    half = len(body) // 2
    split = (
        b"\x16\x03\x01"
        + half.to_bytes(2)
        + body[:half]
        + b"\x16\x03\x01"
        + (len(body) - half).to_bytes(2)
        + body[half:]
    )
    hello = parse_client_hello(split)
    assert hello is not None
    assert hello.sni == "example.org"


@pytest.mark.parametrize(
    "data",
    [
        b"SSH-2.0-OpenSSH_9.6\r\n",
        b"GET / HTTP/1.1\r\n\r\n",
        b"\x16\x02\x00\x00\x05hello",  # wrong major version
        b"\x16\x03\x01\x00\x00",  # empty record
        b"\x16\x03\x01\x00\x04\x02\x00\x00\x00",  # ServerHello, not ClientHello
        b"\x16\x03\x01\x00\x05\x01\x00\x00\x01\x00",  # truncated body
    ],
)
def test_not_tls(data: bytes) -> None:
    with pytest.raises(NotTLS):
        parse_client_hello(data)


def test_minimal_hello_without_extensions() -> None:
    body = b"\x03\x03" + bytes(32) + b"\x00" + b"\x00\x02\x13\x01" + b"\x01\x00"
    message = b"\x01" + len(body).to_bytes(3) + body
    record = b"\x16\x03\x01" + len(message).to_bytes(2) + message
    assert parse_client_hello(record) is not None
