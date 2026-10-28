# Frequently Asked Questions

## Can I intercept every host?

No: only the hosts in `intercept.hosts`, and a lone `*` is refused. Intercepting everything breaks every app that pins certificates, and mints certificates nobody asked for. Everything else is tunnelled untouched. See [In-Depth Configuration](In-Depth-Configuration#the-allowlist).

## Can I turn off verification of the origin's certificate?

No, by design: a MITM proxy that doesn't verify the far end silently downgrades the security of everything through it. For lab services with private certificates, add their CA with `upstream.extra_ca_file`.

## Does it work with HTTP/2?

Yes, on both sides, and the sides needn't match: an HTTP/2 client can reach an HTTP/1.1 origin and the other way round. Middleware sees the same `Flow` either way. HTTP/3 (QUIC) isn't supported.

## How do I write a middleware?

Subclass `Middleware`, register it in `middleware.available`, and name it in `middleware.chain`. See [Writing Middleware](Writing-Middleware).

## Are WebSockets inspected?

No. The upgrade is forwarded and, once it succeeds, the connection is relayed as raw bytes; no middleware runs for WebSocket flows.

## Does it record traffic?

No. There's no flow history or replay. The access log (`proxy.access_log`) writes one line per flow, and your own middleware can store whatever it likes.

## Why does my phone or app still fail after installing the CA?

Some apps pin their certificates, and Android apps ignore user-installed CAs unless they opt in. Leave those hosts off the allowlist and they work untouched.
