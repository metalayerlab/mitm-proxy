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


class FlowLogger(Middleware):
    async def on_request(self, flow: Flow) -> Action:
        flow.metadata["log.started"] = time.monotonic()
        return Action.CONTINUE

    async def on_response(self, flow: Flow) -> Action:
        request, response = flow.request, flow.response
        started = flow.metadata.get("log.started", time.monotonic())
        upstream = flow.server_conn.protocol if flow.server_conn else "-"
        log.info(
            "%s %s>%s %s %s://%s%s %s req=%s resp=%s %.1fms",
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
            (time.monotonic() - started) * 1000,
        )
        return Action.CONTINUE
