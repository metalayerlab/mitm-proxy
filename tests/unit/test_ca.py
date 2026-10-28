import ipaddress
import os
import ssl
import stat
from pathlib import Path

import pytest
from cryptography import x509

from mitm_proxy.errors import CAError
from mitm_proxy.tls.ca import fingerprint, generate_root, load_root, write_root
from tests.helpers import handshake_in_memory, make_ca, trusting_client_context


def test_leaf_names_exactly_the_host() -> None:
    ca = make_ca()
    leaf = ca.mint_leaf("www.example.org")
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["www.example.org"]
    assert leaf.issuer == ca.cert.subject
    assert leaf.not_valid_after_utc <= ca.cert.not_valid_after_utc


def test_ip_leaf_uses_an_ip_san() -> None:
    leaf = make_ca().mint_leaf("192.0.2.10")
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.IPAddress) == [ipaddress.ip_address("192.0.2.10")]


def test_long_host_has_no_common_name_and_a_critical_san() -> None:
    host = ("a" * 60) + ".example.org"
    leaf = make_ca().mint_leaf(host)
    assert list(leaf.subject) == []
    assert leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).critical


def test_a_client_trusting_the_root_accepts_the_leaf() -> None:
    ca = make_ca()
    client, _ = handshake_in_memory(
        trusting_client_context(ca, ["h2"]), ca.server_context("example.org"), "example.org"
    )
    assert client.selected_alpn_protocol() == "h2"


def test_a_leaf_for_another_host_is_rejected() -> None:
    ca = make_ca()
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake_in_memory(
            trusting_client_context(ca), ca.server_context("example.org"), "example.net"
        )


def test_contexts_are_cached_and_evicted() -> None:
    ca = make_ca()  # cache_size=4
    first = ca.server_context("a.example.org")
    assert ca.server_context("a.example.org") is first
    for name in "bcde":
        ca.server_context(f"{name}.example.org")
    assert ca.server_context("a.example.org") is not first
    ca.close()


def test_generate_on_first_load_then_reuse(tmp_path: Path) -> None:
    cert, _ = load_root(
        cert_pem="", key_pem="", state_dir=tmp_path, common_name="lab", generate=True
    )
    key_path = tmp_path / "ca" / "ca-key.pem"
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    again, _ = load_root(
        cert_pem="", key_pem="", state_dir=tmp_path, common_name="x", generate=True
    )
    assert fingerprint(again) == fingerprint(cert)
    assert cert.subject.rfc4514_string() == "CN=lab"


def test_missing_ca_without_generate(tmp_path: Path) -> None:
    with pytest.raises(CAError, match="ca init"):
        load_root(cert_pem="", key_pem="", state_dir=tmp_path, common_name="x", generate=False)


def test_write_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    cert, key = generate_root("one")
    write_root(tmp_path / "ca", cert, key)
    with pytest.raises(CAError, match="--force"):
        write_root(tmp_path / "ca", cert, key)
    other, other_key = generate_root("two")
    write_root(tmp_path / "ca", other, other_key, force=True)
    loaded, _ = load_root(
        cert_pem="", key_pem="", state_dir=tmp_path, common_name="", generate=False
    )
    assert fingerprint(loaded) == fingerprint(other)


def test_loads_from_settings_pem(tmp_path: Path) -> None:
    cert, key = generate_root("settings")
    write_root(tmp_path / "ca", cert, key)
    cert_pem = (tmp_path / "ca" / "ca.pem").read_text()
    key_pem = (tmp_path / "ca" / "ca-key.pem").read_text()
    loaded, _ = load_root(
        cert_pem=cert_pem,
        key_pem=key_pem,
        state_dir=tmp_path / "unused",
        common_name="",
        generate=False,
    )
    assert fingerprint(loaded) == fingerprint(cert)


def test_mismatched_key_is_rejected(tmp_path: Path) -> None:
    cert, key = generate_root("one")
    _, other_key = generate_root("two")
    write_root(tmp_path / "a", cert, key)
    write_root(tmp_path / "b", cert, other_key)
    with pytest.raises(CAError, match="does not belong"):
        load_root(
            cert_pem=(tmp_path / "a" / "ca.pem").read_text(),
            key_pem=(tmp_path / "b" / "ca-key.pem").read_text(),
            state_dir=tmp_path,
            common_name="",
            generate=False,
        )


@pytest.mark.parametrize(
    ("cert_pem", "key_pem", "message"),
    [("junk", "junk", "certificate is not valid PEM"), (None, "junk", "not a valid")],
)
def test_malformed_pem(tmp_path: Path, cert_pem: str | None, key_pem: str, message: str) -> None:
    cert, key = generate_root("x")
    write_root(tmp_path / "ca", cert, key)
    real_cert = (tmp_path / "ca" / "ca.pem").read_text()
    with pytest.raises(CAError, match=message):
        load_root(
            cert_pem=cert_pem or real_cert,
            key_pem=key_pem,
            state_dir=tmp_path,
            common_name="",
            generate=False,
        )


def test_a_leaf_is_not_accepted_as_a_ca(tmp_path: Path) -> None:
    ca = make_ca()
    write_root(tmp_path / "ca", ca.mint_leaf("example.org"), ca._leaf_key)
    with pytest.raises(CAError, match="not a CA certificate"):
        load_root(cert_pem="", key_pem="", state_dir=tmp_path, common_name="", generate=False)


def test_loose_key_permissions_warn(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    load_root(cert_pem="", key_pem="", state_dir=tmp_path, common_name="x", generate=True)
    os.chmod(tmp_path / "ca" / "ca-key.pem", 0o644)
    load_root(cert_pem="", key_pem="", state_dir=tmp_path, common_name="x", generate=False)
    assert "should be 0600" in caplog.text


def test_unreadable_ca_dir(tmp_path: Path) -> None:
    (tmp_path / "ca").mkdir()
    (tmp_path / "ca" / "ca.pem").write_text("x")
    with pytest.raises(CAError, match="cannot read"):
        load_root(cert_pem="", key_pem="", state_dir=tmp_path, common_name="", generate=False)


def test_cannot_write(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    cert, key = generate_root("x")
    with pytest.raises(CAError, match="cannot write"):
        write_root(blocker / "ca", cert, key)
