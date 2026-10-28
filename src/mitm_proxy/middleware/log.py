"""FlowLogger: one INFO line per intercepted flow, and the reference middleware.

It applies to every flow, so every flow it sees is buffered for middleware: it is
not in the default chain. Turn it on with ``middleware.chain: [log]``.
"""

from __future__ import annotations

import logging
import time

from mitm_proxy.http.body import BodyStream, BufferedBodyStream
from mitm_proxy.http.flow import Flow
from mitm_proxy.pipeline import Action, Middleware

log = logging.getLogger("mitm_proxy.flows")


def _size(body: BodyStream) -> str:
    return str(len(body)) if isinstance(body, BufferedBodyStream) else "-"


def _ms(seconds: float | None) -> str:
    return "-" if seconds is None else f"{seconds * 1000:.1f}ms"


class FlowLogger(Middleware):
    async def on_response(self, flow: Flow) -> Action:
        request, response, timings = flow.request, flow.response, flow.timings
        upstream = flow.server_conn.protocol if flow.server_conn else "-"
        # Logged from on_response, before the response goes to the client: total runs to now
        # (the whole response is in), not to the last byte sent.
        connect = _ms(timings.connect)
        if timings.connect is not None and timings.connection_reused:
            connect += "(reused)"
        log.info(
            "%s %s>%s %s %s://%s%s %s req=%s resp=%s total=%s connect=%s ttfb=%s mw=%s",
            flow.id,
            flow.protocol,
            upstream,
            request.method,
            request.scheme,
            request.authority,
            request.path,
            response.status if response else "-",
            _size(request.body),
            _size(response.body) if response else "-",
            _ms(time.monotonic() - timings.started),
            connect,
            _ms(timings.ttfb),
            _ms(timings.middleware_total),
        )
        return Action.CONTINUE
