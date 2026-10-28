"""The certificate authority: the root CA, and the leaf certificates it mints per host.

One ECDSA P-256 leaf key is generated at startup and shared by every leaf, so
minting a leaf is one signature: fast enough to do synchronously, in-process.
Leaves name exactly one host (never a wildcard) and are cached, as ready-made
server SSLContexts, in an LRU keyed by the normalized host alone (h1 and h2
share them).

``ssl`` can only load certificates from files, so each leaf is written to a
private temporary directory for the moment it takes to load. The shared leaf key
stays there for the CA's lifetime, encrypted with a random password that only
lives in memory.
"""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import logging
import os
import secrets
import shutil
import ssl
import stat
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from mitm_proxy.errors import CAError

log = logging.getLogger(__name__)

ROOT_VALIDITY = datetime.timedelta(days=3650)
LEAF_VALIDITY = datetime.timedelta(days=30)
BACKDATE = datetime.timedelta(days=1)
CERT_FILE = "ca.pem"
KEY_FILE = "ca-key.pem"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def generate_root(common_name: str) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = _now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - BACKDATE)
        .not_valid_after(now + ROOT_VALIDITY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert, key


def fingerprint(cert: x509.Certificate) -> str:
    """SHA-256 fingerprint, colon-separated upper-case hex (as browsers show it)."""
    return cert.fingerprint(hashes.SHA256()).hex(":").upper()


def _load_pair(
    cert_pem: bytes, key_pem: bytes, where: str
) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    try:
        cert = x509.load_pem_x509_certificate(cert_pem)
    except ValueError as e:
        raise CAError(f"{where}: the CA certificate is not valid PEM: {e}") from None
    try:
        key = serialization.load_pem_private_key(key_pem, password=None)
    except (ValueError, TypeError) as e:
        raise CAError(f"{where}: the CA key is not a valid, unencrypted PEM key: {e}") from None
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise CAError(f"{where}: the CA key must be an EC key")
    spki = (serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if key.public_key().public_bytes(*spki) != cert.public_key().public_bytes(*spki):
        raise CAError(f"{where}: the CA key does not belong to the CA certificate")
    try:
        constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound:
        constraints = None
    if constraints is None or not constraints.ca:
        raise CAError(f"{where}: the certificate is not a CA certificate")
    return cert, key


def write_root(
    ca_dir: Path, cert: x509.Certificate, key: ec.EllipticCurvePrivateKey, *, force: bool = False
) -> None:
    """Write the root to ca_dir: the key with mode 0600, created exclusively unless force."""
    try:
        ca_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        key_path, cert_path = ca_dir / KEY_FILE, ca_dir / CERT_FILE
        flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if force else os.O_EXCL)
        key_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        fd = os.open(key_path, flags, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key_pem)
        os.chmod(key_path, 0o600)
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    except FileExistsError:
        raise CAError(f"{ca_dir}: a CA already exists (use --force to replace it)") from None
    except OSError as e:
        raise CAError(f"cannot write the CA to {ca_dir}: {e.strerror}") from e


def load_root(
    *,
    cert_pem: str,
    key_pem: str,
    state_dir: Path,
    common_name: str,
    generate: bool,
) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    """The root CA: from the settings, else <state_dir>/ca/, else (if generate) a new one."""
    if cert_pem and key_pem:
        return _load_pair(cert_pem.encode(), key_pem.encode(), "ca.cert/ca.key")
    ca_dir = state_dir / "ca"
    cert_path, key_path = ca_dir / CERT_FILE, ca_dir / KEY_FILE
    if cert_path.exists() or key_path.exists():
        try:
            mode = stat.S_IMODE(key_path.stat().st_mode)
            cert, key = _load_pair(cert_path.read_bytes(), key_path.read_bytes(), str(ca_dir))
        except OSError as e:
            raise CAError(f"cannot read the CA in {ca_dir}: {e.strerror}") from e
        if mode & 0o077:
            log.warning("%s is readable by others (mode %o); it should be 0600", key_path, mode)
        return cert, key
    if not generate:
        raise CAError(f"no CA in {ca_dir} (run `mitm-proxy ca init`)")
    cert, key = generate_root(common_name)
    write_root(ca_dir, cert, key)
    log.info("generated a new root CA in %s, SHA-256 %s", ca_dir, fingerprint(cert))
    return cert, key


class CertificateAuthority:
    def __init__(
        self,
        cert: x509.Certificate,
        key: ec.EllipticCurvePrivateKey,
        *,
        cache_size: int = 1024,
        alpn: tuple[str, ...] = ("h2", "http/1.1"),
    ) -> None:
        self.cert = cert
        self._key = key
        self._cache_size = cache_size
        self._alpn = list(alpn)
        self._contexts: OrderedDict[str, ssl.SSLContext] = OrderedDict()
        self._lock = threading.Lock()
        self._leaf_key = ec.generate_private_key(ec.SECP256R1())
        self._ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
        self._dir = Path(tempfile.mkdtemp(prefix="mitm-proxy-leaves-"))
        self._password = secrets.token_bytes(32)
        self._key_path = self._dir / "leaf-key.pem"
        fd = os.open(self._key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(
                self._leaf_key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.BestAvailableEncryption(self._password),
                )
            )

    @property
    def cert_pem(self) -> bytes:
        return self.cert.public_bytes(serialization.Encoding.PEM)

    @property
    def cert_der(self) -> bytes:
        return self.cert.public_bytes(serialization.Encoding.DER)

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.cert)

    def mint_leaf(self, host: str) -> x509.Certificate:
        """A leaf certificate for exactly host (a normalized name or IP literal)."""
        now = _now()
        not_after = min(now + LEAF_VALIDITY, self.cert.not_valid_after_utc)
        try:
            san: x509.GeneralName = x509.IPAddress(ipaddress.ip_address(host))
        except ValueError:
            san = x509.DNSName(host)
        attributes = [x509.NameAttribute(NameOID.COMMON_NAME, host)] if len(host) <= 64 else []
        return (
            x509.CertificateBuilder()
            .subject_name(x509.Name(attributes))
            .issuer_name(self.cert.subject)
            .public_key(self._leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - BACKDATE)
            .not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([san]), critical=not attributes)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(self._ski),
                critical=False,
            )
            .sign(self._key, hashes.SHA256())
        )

    def server_context(self, host: str) -> ssl.SSLContext:
        """A client-facing server SSLContext presenting a leaf for host, from the cache."""
        with self._lock:
            ctx = self._contexts.get(host)
            if ctx is not None:
                self._contexts.move_to_end(host)
                return ctx
        leaf = self.mint_leaf(host)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.set_alpn_protocols(self._alpn)
        chain = leaf.public_bytes(serialization.Encoding.PEM) + self.cert_pem
        name = hashlib.sha256(host.encode()).hexdigest()[:16] + secrets.token_hex(4)
        chain_path = self._dir / f"{name}.pem"
        chain_path.write_bytes(chain)
        try:
            ctx.load_cert_chain(chain_path, self._key_path, password=self._password)
        finally:
            chain_path.unlink(missing_ok=True)
        with self._lock:
            self._contexts[host] = ctx
            while len(self._contexts) > self._cache_size:
                self._contexts.popitem(last=False)
        return ctx

    def close(self) -> None:
        shutil.rmtree(self._dir, ignore_errors=True)
