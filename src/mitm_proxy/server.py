"""The running proxy: the proxy listener and the admin API, on one event loop.

Configuration is held as an immutable Snapshot and replaced whole by apply():
each new connection reads the snapshot current when it arrives, and keeps it to
the end. Today only startup applies one; the control-plane client (design 0002)
will apply more. service.py runs this next to the admin API.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import hashlib
import ipaddress
import json
import logging
import ssl
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from mitm_proxy import __version__
from mitm_proxy.classify import Decision, decide, peek
from mitm_proxy.config import SETTINGS, Config, ReverseListener, Secret, Section
from mitm_proxy.errors import StartupError, UpstreamError, UpstreamTimeout
from mitm_proxy.exchange import ExchangeContext, handle, log_refusal, refusal_headers
from mitm_proxy.http.flow import ClientConn
from mitm_proxy.http.h1 import H1Server
from mitm_proxy.http.h2 import H2Server
from mitm_proxy.http.messages import IncomingRequest, Responder
from mitm_proxy.interception.base import InterceptedConnection
from mitm_proxy.interception.explicit import ExplicitProxySource
from mitm_proxy.interception.reverse import ReverseProxySource
from mitm_proxy.metrics import Metrics
from mitm_proxy.middleware.capture import Capture
from mitm_proxy.pipeline import Middleware, Pipeline, load_middleware
from mitm_proxy.relay import relay
from mitm_proxy.routing import Route, Router
from mitm_proxy.scope import Allowlist, normalize_host
from mitm_proxy.streams import PlainStream
from mitm_proxy.tls.ca import CertificateAuthority, load_root
from mitm_proxy.tls.terminate import TLSStream
from mitm_proxy.tls.upstream import UpstreamContexts
from mitm_proxy.upstream.chain import RoutingConnector
from mitm_proxy.upstream.pool import ConnectionPool, Connector, Target, TCPConnector

log = logging.getLogger(__name__)
relayed_log = logging.getLogger("mitm_proxy.relayed")  # proxy.access_log_relayed (0005)


def _canonical(node: object) -> Any:
    """A value as JSON can hold it; a secret (a setting or a section's leaf) only as set/unset."""
    if isinstance(node, Secret):
        return {"set": bool(node)}
    if isinstance(node, Section):
        return {key: _canonical(child) for key, child in node.items()}
    if isinstance(node, tuple):
        return [_canonical(item) for item in node]
    if isinstance(node, datetime.date):
        return node.isoformat()
    return node


def config_hash(config: Config) -> str:
    """sha256 over the canonical JSON of the settings; secrets count only as set/unset."""
    secret = {s.key for s in SETTINGS if s.secret}
    values: dict[str, Any] = {}
    for key, value in sorted(config.values.items()):
        if key in secret:
            values[key] = {"set": bool(value)}
        else:
            values[key] = _canonical(value)
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class Snapshot:
    config: Config
    allowlist: Allowlist
    pipeline: Pipeline
    hash: str
    router: Router = field(default_factory=Router)  # design 0013: exits empty, all direct
    applied_at: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC)
    )


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ProxyServer:
    def __init__(
        self,
        config: Config,
        *,
        middleware: Sequence[tuple[str, Middleware]] | None = None,
        connector: Connector | None = None,
    ) -> None:
        self.metrics = Metrics(__version__)
        cert, key = load_root(
            cert_pem=config.ca_cert,
            key_pem=config.ca_key.reveal(),
            state_dir=config.state_dir,
            common_name=config.ca_common_name,
            generate=True,
        )
        self.ca = CertificateAuthority(cert, key, cache_size=config.leaf_cache_size)
        self.metrics.certificate_expiry.set(cert.not_valid_after_utc.timestamp(), "ca")
        # proxy.idle_timeout_ms also bounds socket writes, both ways: a peer that stops
        # reading would otherwise hold its flow (and connection) forever.
        idle = config.idle_timeout_ms / 1000
        # Direct, or through the target's exit (design 0013): with exits empty, always direct.
        self.connector = connector or RoutingConnector(
            UpstreamContexts(config.upstream_extra_ca_file), write_timeout=idle
        )
        self.pool = ConnectionPool(
            self.connector,
            max_per_host=config.upstream_max_connections_per_host,
            idle_timeout=config.upstream_idle_timeout_ms / 1000,
            connect_timeout=config.upstream_connect_timeout_ms / 1000,
            # Waiting on an origin's bytes has its own setting (design 0012): a slow origin
            # needs a long one, and the client side's idle timeout stays short.
            read_timeout=config.upstream_read_timeout_ms / 1000,
        )
        self.metrics.upstream_connections.read = lambda: {
            (protocol,): float(n) for protocol, n in self.pool.counts().items()
        }
        self.snapshot = self._build(config, middleware)
        self.metrics.config_applies.inc("applied")
        self.state = "starting"
        self.started_at = datetime.datetime.now(datetime.UTC)
        self.source = ExplicitProxySource(
            on_tunnel=self.handle_tunnel,
            plain_context=self._plain_context,
            auth=lambda: self.snapshot.config.proxy_auth.reveal(),
            idle_timeout=config.idle_timeout_ms / 1000,
            on_auth_failure=self.metrics.proxy_auth_failures.inc,
        )
        # Reverse listeners are bootstrap (design 0012): built from the startup config, bound in
        # start(), and a change to `reverse` applies only on restart. Each forwards every request
        # to its one upstream; the context reads the current snapshot, like the forward proxy's.
        self.reverse_sources: list[tuple[ReverseListener, ReverseProxySource]] = [
            (
                listener,
                ReverseProxySource(
                    context=self._reverse_context(listener),
                    upstream_authority=listener.upstream_authority,
                    upstream_scheme="https" if listener.upstream_tls else "http",
                    idle_timeout=config.idle_timeout_ms / 1000,
                ),
            )
            for listener in config.reverse
        ]
        self._server: asyncio.Server | None = None
        self._reverse_servers: list[asyncio.Server] = []
        self._connections: set[asyncio.Task[Any]] = set()

    def _build(
        self, config: Config, middleware: Sequence[tuple[str, Middleware]] | None
    ) -> Snapshot:
        loaded = (
            list(middleware)
            if middleware is not None
            else load_middleware(
                config.middleware_available, config.middleware_chain, config.middleware_config
            )
        )
        # Before the pipeline exists: every name it can report is already a declared label
        # value. The whole registry, so the names a later chain may use are covered too (0009).
        self.metrics.declare_middleware([*config.middleware_available, *(n for n, _ in loaded)])
        for _, mw in loaded:
            if isinstance(mw, Capture):
                # Its rule names are label values before it can count a record (0009).
                self.metrics.declare_capture_rules(mw.rule_names)
                mw.on_record = self.metrics.capture_records.inc
        pipeline = Pipeline(loaded, hook_timeout=config.hook_timeout_ms / 1000)
        router = Router(config.exits, config.routing_default_via)
        if router.enabled:
            # Its exit names are label values before anything is refused (0009).
            self.metrics.enable_routing(router.exits)
        return Snapshot(
            config, Allowlist(config.intercept_hosts), pipeline, config_hash(config), router
        )

    def apply(self, config: Config) -> None:
        """Swap in a new configuration; connections already open keep the old one."""
        if config.reverse != self.snapshot.config.reverse:
            # Reverse listeners are bound once, at startup (design 0012): a change needs a restart.
            log.warning(
                "reverse changed; the reverse listeners keep their configuration until restart"
            )
        self.snapshot = self._build(config, None)
        # The pool and the connector are shared, not part of the snapshot: give them the new
        # values. They're read when used, so they apply to what happens from now on.
        self.pool.max_per_host = config.upstream_max_connections_per_host
        self.pool.idle_timeout = config.upstream_idle_timeout_ms / 1000
        self.pool.connect_timeout = config.upstream_connect_timeout_ms / 1000
        self.pool.read_timeout = config.upstream_read_timeout_ms / 1000
        if isinstance(self.connector, TCPConnector):
            self.connector.write_timeout = config.idle_timeout_ms / 1000
        self.metrics.config_applies.inc("applied")

    # The listener

    async def start(self) -> None:
        config = self.snapshot.config
        try:
            self._server = await asyncio.start_server(
                self._on_client, config.proxy_host, config.proxy_port
            )
        except OSError as e:
            raise StartupError(
                f"cannot listen on {config.proxy_host}:{config.proxy_port}: {e.strerror or e}"
            ) from e
        if not _is_loopback(config.proxy_host) and not config.proxy_auth:
            log.warning(
                "the proxy listens on %s without proxy.auth: anyone who can reach it can use it "
                "(an open proxy)",
                config.proxy_host,
            )
        log.info(
            "proxy listening on %s:%d; root CA SHA-256 %s",
            config.proxy_host,
            config.proxy_port,
            self.ca.fingerprint,
        )
        for listener, source in self.reverse_sources:
            try:
                server = await asyncio.start_server(
                    self._client_handler(source), listener.listen_host, listener.listen_port
                )
            except OSError as e:
                raise StartupError(
                    f"cannot listen on {listener.listen_host}:{listener.listen_port} "
                    f"for the reverse proxy: {e.strerror or e}"
                ) from e
            self._reverse_servers.append(server)
            scheme = "https" if listener.upstream_tls else "http"
            log.info(
                "reverse proxy listening on %s:%d -> %s://%s",
                listener.listen_host,
                listener.listen_port,
                scheme,
                listener.upstream_authority,
            )
        # Ready only once every listener, the proxy and the reverse ones, is bound (design 0012).
        self.state = "ready"

    @property
    def client_write_timeout(self) -> float:
        """Taken from the snapshot when a client connects, like every setting."""
        return self.snapshot.config.idle_timeout_ms / 1000

    @property
    def port(self) -> int:
        """The bound port (useful when proxy.port is 0 in tests)."""
        if self._server is None or not self._server.sockets:
            return 0
        return int(self._server.sockets[0].getsockname()[1])

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await self._serve(self.source.handle_client, reader, writer)

    def _client_handler(
        self, source: ReverseProxySource
    ) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]:
        """A listener callback for a reverse source, sharing the proxy's connection tracking."""

        async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await self._serve(source.handle_client, reader, writer)

        return on_client

    async def _serve(
        self,
        handle_client: Callable[[PlainStream, tuple[str, int]], Awaitable[None]],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._connections.add(task)
        stream = PlainStream(reader, writer, write_timeout=self.client_write_timeout)
        self.metrics.client_connections.add(1)
        try:
            await handle_client(stream, stream.peer())
        except Exception:
            log.exception("client connection failed")
        finally:
            self.metrics.client_connections.add(-1)
            try:
                await stream.close()
            finally:
                # Only now: stop() must still be able to cancel a connection that's closing.
                if task is not None:
                    self._connections.discard(task)

    def _reverse_context(
        self, listener: ReverseListener
    ) -> Callable[[tuple[str, int]], ExchangeContext]:
        """A context factory for one reverse listener: the fixed upstream, the current snapshot."""

        def make(client_addr: tuple[str, int]) -> ExchangeContext:
            snap = self.snapshot
            # Routed like every other upstream connection (design 0013): never direct when
            # exits are set, unless routing.allow_direct chose it.
            route = snap.router.route(listener.upstream_host)
            return ExchangeContext(
                pool=self.pool,
                metrics=self.metrics,
                client_conn=ClientConn(client_addr),
                protocol="http/1.1",
                max_buffer=snap.config.max_buffer_bytes,
                idle_timeout=snap.config.idle_timeout_ms / 1000,
                pipeline=snap.pipeline,
                target=Target(
                    listener.upstream_host,
                    listener.upstream_port,
                    tls=listener.upstream_tls,
                    sni=listener.upstream_host,
                    via=route.exit,
                ),
                fixed_route=route,
                scope=None,
                direction="inbound",
                server_timing=snap.config.server_timing,
                access_log=snap.config.access_log,
                access_log_relayed=snap.config.access_log_relayed,
            )

        return make

    def _plain_context(self, client_addr: tuple[str, int]) -> ExchangeContext:
        snap = self.snapshot
        return ExchangeContext(
            pool=self.pool,
            metrics=self.metrics,
            client_conn=ClientConn(client_addr),
            protocol="http/1.1",
            max_buffer=snap.config.max_buffer_bytes,
            idle_timeout=snap.config.idle_timeout_ms / 1000,
            pipeline=snap.pipeline,
            target=None,
            scope=snap.allowlist,
            router=snap.router,
            server_timing=snap.config.server_timing,
            access_log=snap.config.access_log,
            access_log_relayed=snap.config.access_log_relayed,
        )

    # Tunnels

    async def handle_tunnel(self, conn: InterceptedConnection) -> None:
        snap = self.snapshot
        config = snap.config
        idle = config.idle_timeout_ms / 1000
        logged = config.access_log_relayed
        route = snap.router.route(conn.target_host)
        decision = decide(conn.target_host, snap.allowlist, None)
        if decision is Decision.RELAY_OUT_OF_SCOPE:
            if route.refused is not None:
                await self._refuse_tunnel(conn, route)
                return
            await self._relay(conn, b"", decision, route, accepted=False, idle=idle, logged=logged)
            return
        await conn.accept()
        peeked = await peek(conn.stream, config.peek_timeout_ms / 1000)
        decision = decide(conn.target_host, snap.allowlist, peeked)
        if decision is not Decision.INTERCEPT or peeked.hello is None:
            if route.refused is not None:
                # Already accepted, so there is no 503 to send: close it, never relay it.
                log_refusal(self.metrics, conn.target_host, route)
                await conn.stream.close()
                return
            await self._relay(
                conn, peeked.data, decision, route, accepted=True, idle=idle, logged=logged
            )
            return

        sni = peeked.hello.sni
        try:
            cert_host = normalize_host(sni or conn.target_host)
        except ValueError:
            if route.refused is not None:
                log_refusal(self.metrics, conn.target_host, route)
                await conn.stream.close()
                return
            await self._relay(
                conn,
                peeked.data,
                Decision.RELAY_NOT_TLS,
                route,
                accepted=True,
                idle=idle,
                logged=logged,
            )
            return
        tls = TLSStream(
            conn.stream, self.ca.server_context(cert_host), server_side=True, initial=peeked.data
        )
        try:
            await asyncio.wait_for(tls.handshake(), idle)
        except (ssl.SSLError, ConnectionError, OSError, TimeoutError) as e:
            log.info(
                "TLS handshake with %s for %s failed (%s): if the client rejected the "
                "certificate, install the root CA on it",
                conn.client_addr[0],
                cert_host,
                e,
            )
            await conn.stream.close()
            return
        protocol = tls.alpn or "http/1.1"
        ctx = ExchangeContext(
            pool=self.pool,
            metrics=self.metrics,
            client_conn=ClientConn(
                conn.client_addr, sni, peeked.hello.alpn, tls.version, tls.cipher
            ),
            protocol=protocol,
            max_buffer=config.max_buffer_bytes,
            idle_timeout=idle,
            pipeline=snap.pipeline,
            target=Target(conn.target_host, conn.target_port, tls=True, sni=sni, via=route.exit),
            # Refused (design 0013): each request gets a local 503, and nothing is sent anywhere.
            fixed_route=route,
            server_timing=config.server_timing,
            access_log=config.access_log,
            access_log_relayed=config.access_log_relayed,
        )

        async def handler(incoming: IncomingRequest, responder: Responder) -> None:
            await handle(ctx, incoming, responder)

        if protocol == "h2":
            await H2Server(tls, handler, idle_timeout=idle).run()
        else:
            authority = (
                conn.target_host
                if conn.target_port == 443
                else f"{conn.target_host}:{conn.target_port}"
            )
            await H1Server(
                tls,
                handler,
                idle_timeout=idle,
                client_addr=conn.client_addr,
                default_scheme="https",
                default_authority=authority,
            ).run()

    async def _relay(
        self,
        conn: InterceptedConnection,
        peeked: bytes,
        decision: Decision,
        route: Route,
        *,
        accepted: bool,
        idle: float,
        logged: bool = False,
    ) -> None:
        self.metrics.tunnels.inc(decision.value)
        target = Target(conn.target_host, conn.target_port, tls=False, via=route.exit, tunnel=True)
        started = time.monotonic()
        reason, counts = decision.value, (0, 0)
        try:
            try:
                upstream = await self.connector(target, (), self.pool.connect_timeout)
            except UpstreamError as e:
                log.info("tunnel to %s:%d failed: %s", conn.target_host, conn.target_port, e)
                status = 504 if isinstance(e, UpstreamTimeout) else 502
                reason = f"error:{status}"
                if accepted:
                    await conn.stream.close()
                else:
                    await conn.reject(status, str(e))
                return
            if not accepted:
                await conn.accept()
            if peeked:
                await upstream.stream.write(peeked)
            up, down = await relay(conn.stream, upstream.stream, idle_timeout=idle)
            counts = (len(peeked) + up, down)
        finally:
            if logged:
                # Synchronous, so a tunnel cut short by shutdown is still logged.
                relayed_log.info(
                    "tunnel %s %s:%d %s:%d %s up=%d down=%d total=%.1fms",
                    uuid.uuid4().hex,
                    conn.client_addr[0],
                    conn.client_addr[1],
                    conn.target_host,
                    conn.target_port,
                    reason,
                    counts[0],
                    counts[1],
                    (time.monotonic() - started) * 1000,
                )

    async def _refuse_tunnel(self, conn: InterceptedConnection, route: Route) -> None:
        """A CONNECT with no exit to leave by: 503, Retry-After and Proxy-Status (0013, 9)."""
        log_refusal(self.metrics, conn.target_host, route)
        message = f"not sent to {conn.target_host}: {route.detail}"
        if conn.reject_with is not None:
            await conn.reject_with(503, message, refusal_headers(route))
        else:
            await conn.reject(503, message)

    # Shutdown and status

    async def stop(self, grace: float | None = None) -> None:
        if self.state == "stopped":
            return
        self.state = "stopping"
        if grace is None:
            grace = self.snapshot.config.shutdown_grace_ms / 1000
        listeners = [s for s in (self._server, *self._reverse_servers) if s is not None]
        for server in listeners:
            server.close()
        if self._connections:
            _, pending = await asyncio.wait(list(self._connections), timeout=grace)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        for server in listeners:
            with contextlib.suppress(Exception):
                await server.wait_closed()
        await self.pool.close()
        self.ca.close()
        self.state = "stopped"

    def status(self) -> dict[str, Any]:
        snap = self.snapshot
        listening = self._server is not None and self._server.is_serving()
        return {
            "version": __version__,
            "mode": "standalone",
            "state": self.state,
            "started_at": self.started_at.isoformat(),
            "config": {
                "hash": snap.hash,
                "status": "APPLIED",
                "applied_at": snap.applied_at.isoformat(),
            },
            "middleware": snap.pipeline.names,
            "health": {
                "proxy_listener": {
                    "healthy": listening,
                    "status": "listening" if listening else self.state,
                },
                "ca": {"healthy": True, "status": "loaded"},
            },
            "certificates": {
                "ca": {
                    "not_after": self.ca.cert.not_valid_after_utc.isoformat(),
                    "fingerprint": self.ca.fingerprint,
                }
            },
        }
