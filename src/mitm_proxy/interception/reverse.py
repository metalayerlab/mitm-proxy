"""The reverse-proxy source (design 0012): a plain HTTP/1.1 listener with one fixed upstream.

Clients connect to the listener's address as they would to any server (no CONNECT, no proxy
configuration) and send origin-form requests. Each request is rewritten to the upstream and run
through the same exchange as everything else, so middleware, the access log, Server-Timing,
metrics and upstream verification work unchanged. The context carries a fixed ``target`` (the
upstream) and ``direction="inbound"``; TLS and HTTP/2 on the listener are later (design 0012).
"""

from __future__ import annotations

from collections.abc import Callable

from mitm_proxy.exchange import ExchangeContext, handle
from mitm_proxy.http.h1 import H1Server
from mitm_proxy.http.messages import IncomingRequest, Responder
from mitm_proxy.streams import ByteStream


class ReverseProxySource:
    def __init__(
        self,
        *,
        context: Callable[[tuple[str, int]], ExchangeContext],
        upstream_authority: str,
        upstream_scheme: str,
        idle_timeout: float,
    ) -> None:
        self.context = context
        self.upstream_authority = upstream_authority
        self.upstream_scheme = upstream_scheme
        self.idle_timeout = idle_timeout

    async def handle_client(self, stream: ByteStream, client_addr: tuple[str, int]) -> None:
        ctx = self.context(client_addr)

        async def serve(incoming: IncomingRequest, responder: Responder) -> None:
            self._forward(incoming, client_addr)
            await handle(ctx, incoming, responder)

        # No ProxyMode: origin-form requests, no CONNECT, no proxy authentication. The upstream
        # authority is the default, so a request without a Host still goes to the upstream.
        server = H1Server(
            stream,
            serve,
            idle_timeout=self.idle_timeout,
            client_addr=client_addr,
            default_scheme=self.upstream_scheme,
            default_authority=self.upstream_authority,
        )
        await server.run()

    def _forward(self, incoming: IncomingRequest, client_addr: tuple[str, int]) -> None:
        """Rewrite the request for the upstream and add the forwarding headers (design 0012).

        The authority (and so the origin's ``Host``) becomes the upstream's; the client's own
        ``Host`` is kept in ``X-Forwarded-Host`` when that header is absent, and the client's
        address is appended to ``X-Forwarded-For``. No ``Forwarded`` and no ``X-Forwarded-Proto``.
        """
        request = incoming.request
        client_host = incoming.raw_headers.get("host")
        request.authority = self.upstream_authority
        request.scheme = self.upstream_scheme
        if client_host and "x-forwarded-host" not in request.headers:
            request.headers.set("x-forwarded-host", client_host)
        existing = request.headers.get("x-forwarded-for")
        request.headers.set(
            "x-forwarded-for",
            f"{existing}, {client_addr[0]}" if existing else client_addr[0],
        )
