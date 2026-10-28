import asyncio

from mitm_proxy.classify import Decision, Peeked, decide, peek
from mitm_proxy.scope import Allowlist
from mitm_proxy.tls.clienthello import ClientHello
from tests.helpers import client_hello_bytes, memory_pair

ALLOW = Allowlist(["example.org", "*.example.org"])


def test_decide() -> None:
    hello = ClientHello(sni="example.org", alpn=("h2",))
    assert decide("example.net", ALLOW, None) is Decision.RELAY_OUT_OF_SCOPE
    assert decide("example.org", ALLOW, None) is Decision.INTERCEPT
    assert decide("example.org", ALLOW, Peeked(b"x", None)) is Decision.RELAY_NOT_TLS
    other = ClientHello(sni="example.net", alpn=())
    assert decide("example.org", ALLOW, Peeked(b"x", other)) is Decision.RELAY_SNI_OUT_OF_SCOPE
    assert decide("a.example.org", ALLOW, Peeked(b"x", hello)) is Decision.INTERCEPT
    no_sni = ClientHello(sni=None, alpn=())
    assert decide("example.org", ALLOW, Peeked(b"x", no_sni)) is Decision.INTERCEPT


async def test_peek_reads_a_whole_hello_in_pieces() -> None:
    client, proxy = memory_pair()
    data = client_hello_bytes("example.org")

    async def dribble() -> None:
        for i in range(0, len(data), 100):
            await client.write(data[i : i + 100])
            await asyncio.sleep(0)

    task = asyncio.create_task(dribble())
    peeked = await peek(proxy, 1)
    await task
    assert peeked.data == data and peeked.hello is not None


async def test_peek_gives_up_on_silence_eof_and_garbage() -> None:
    client, proxy = memory_pair()
    assert await peek(proxy, 0.02) == Peeked(b"", None)
    await client.write(b"SSH-2.0-x\r\n")
    assert await peek(proxy, 1) == Peeked(b"SSH-2.0-x\r\n", None)
    client2, proxy2 = memory_pair()
    await client2.write(b"\x16\x03\x01")
    client2.write_eof()
    assert await peek(proxy2, 1) == Peeked(b"\x16\x03\x01", None)
    client3, proxy3 = memory_pair()
    await client3.write(b"\x16\x03\x01\x10\x00")
    assert (await peek(proxy3, 1, limit=4)).hello is None
