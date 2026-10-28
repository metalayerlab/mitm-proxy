"""Parse a TLS ClientHello from the first bytes of a tunnel, without consuming them.

This one parse answers both "is this TLS?" (for the intercept-or-relay decision)
and "which name and protocols does the client want?" (SNI for the leaf
certificate, ALPN for logging). It is a pure function on bytes: the caller keeps
the bytes and replays them into the TLS handshake.
"""

from __future__ import annotations

from dataclasses import dataclass

_HANDSHAKE = 22
_CLIENT_HELLO = 1
_EXT_SERVER_NAME = 0
_EXT_ALPN = 16
_MAX_RECORD = 16384 + 2048  # TLSCiphertext limit; a ClientHello record is plaintext and smaller


class NotTLS(Exception):
    """The bytes are not the start of a TLS handshake."""


@dataclass(frozen=True)
class ClientHello:
    sni: str | None
    alpn: tuple[str, ...]


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise NotTLS("truncated ClientHello")
        chunk = self.data[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return int.from_bytes(self.take(2))

    def vector(self, length_bytes: int) -> _Reader:
        n = int.from_bytes(self.take(length_bytes))
        return _Reader(self.take(n))

    def done(self) -> bool:
        return self.pos >= len(self.data)


def parse_client_hello(data: bytes) -> ClientHello | None:
    """The ClientHello in data; None if more bytes are needed; NotTLS if it isn't TLS.

    The handshake message may span several records, and is reassembled.
    """
    if data and data[0] != _HANDSHAKE:
        raise NotTLS("not a TLS handshake record")
    handshake = bytearray()
    offset = 0
    while True:
        if len(data) < offset + 5:
            return None
        content_type, major = data[offset], data[offset + 1]
        length = int.from_bytes(data[offset + 3 : offset + 5])
        if content_type != _HANDSHAKE or major != 3 or length == 0 or length > _MAX_RECORD:
            raise NotTLS("not a TLS handshake record")
        if len(data) < offset + 5 + length:
            return None
        handshake += data[offset + 5 : offset + 5 + length]
        offset += 5 + length
        if len(handshake) >= 4:
            if handshake[0] != _CLIENT_HELLO:
                raise NotTLS("first handshake message is not a ClientHello")
            body_length = int.from_bytes(handshake[1:4])
            if len(handshake) >= 4 + body_length:
                return _parse_body(bytes(handshake[4 : 4 + body_length]))


def _parse_body(body: bytes) -> ClientHello:
    r = _Reader(body)
    r.take(2)  # legacy_version
    r.take(32)  # random
    r.vector(1)  # legacy_session_id
    r.vector(2)  # cipher_suites
    r.vector(1)  # legacy_compression_methods
    sni: str | None = None
    alpn: tuple[str, ...] = ()
    if r.done():
        return ClientHello(sni=None, alpn=())
    extensions = r.vector(2)
    while not extensions.done():
        ext_type = extensions.u16()
        ext = extensions.vector(2)
        if ext_type == _EXT_SERVER_NAME:
            names = ext.vector(2)
            while not names.done():
                name_type = names.u8()
                name = names.vector(2)
                if name_type == 0:
                    try:
                        sni = name.data.decode("ascii")
                    except UnicodeDecodeError:
                        raise NotTLS("server name is not ASCII") from None
        elif ext_type == _EXT_ALPN:
            protocols = ext.vector(2)
            found: list[str] = []
            while not protocols.done():
                found.append(protocols.vector(1).data.decode("ascii", "replace"))
            alpn = tuple(found)
    return ClientHello(sni=sni, alpn=alpn)
