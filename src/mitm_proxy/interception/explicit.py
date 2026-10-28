"""The explicit-proxy source (Phase 1): clients are configured to use the proxy.

Every client connection starts as HTTP/1.1 in proxy mode. Absolute-form requests
(``GET http://example.org/``) are proxied there and then, through the same
exchange as everything else. A ``CONNECT host:port`` becomes an
InterceptedConnection for the tunnel handler, which answers it (200, or an error
after trying the origin) and decides what happens to the tunnel.
"""

from __future__ import annotations

from collections.abc import Callable

from mitm_proxy.exchange import ExchangeContext, handle
from mitm_proxy.http.h1 import H1Server, ProxyMode
from mitm_proxy.http.messages import IncomingRequest, Responder
from mitm_proxy.interception.base import InterceptedConnection, TunnelHandler
from mitm_proxy.streams import ByteStream


class ExplicitProxySource:
    def __init__(
        self,
        *,
        on_tunnel: TunnelHandler,
        plain_context: Callable[[tuple[str, int]], ExchangeContext],
        auth: Callable[[], str],
        idle_timeout: float,
        on_auth_failure: Callable[[], None] | None = None,
    ) -> None:
        self.on_tunnel = on_tunnel
        self.plain_context = plain_context
        self.auth = auth
        self.idle_timeout = idle_timeout
        self.on_auth_failure = on_auth_failure

    async def handle_client(self, stream: ByteStream, client_addr: tuple[str, int]) -> None:
        ctx = self.plain_context(client_addr)

        async def plain(incoming: IncomingRequest, responder: Responder) -> None:
            await handle(ctx, incoming, responder)

        async def tunnel(conn: InterceptedConnection) -> None:
            conn.client_addr = client_addr
            await self.on_tunnel(conn)

        server = H1Server(
            stream,
            plain,
            idle_timeout=self.idle_timeout,
            client_addr=client_addr,
            default_scheme="http",
            proxy=ProxyMode(
                auth=self.auth(), on_connect=tunnel, on_auth_failure=self.on_auth_failure
            ),
        )
        await server.run()
