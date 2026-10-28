# mitm-proxy

Async Python MITM proxy with a self-signed CA — HTTP/1.1 & HTTP/2, pluggable middleware for inspecting and rewriting traffic in flight.

mitm-proxy is a lab tool for seeing and changing HTTP(S) traffic while it happens. Point a client at it, trust its root CA, list the hosts you want to look inside, and every request and response to those hosts runs through middleware you write in Python: log it, rewrite it, block it, or answer it yourself. Everything else passes through untouched. Both HTTP/1.1 and HTTP/2 work on either side, and the two sides needn't match.

```sh
$ MITM_PROXY_INTERCEPT_HOSTS=example.org MITM_PROXY_PROXY_ACCESS_LOG=true mitm-proxy serve &
$ curl -s http://127.0.0.1:8080/ca.pem -o mitm-ca.pem          # the root CA to trust
$ curl --proxy http://127.0.0.1:3128 --cacert mitm-ca.pem https://example.org/
```

The proxy logs one line per intercepted request (client protocol > origin protocol):

```text
INFO mitm_proxy.flows: 9ae147a67a5e4b78 http/1.1>h2 GET https://example.org/ 200 req=0 resp=1256 total=41.3ms connect=0.0ms(reused) ttfb=29.1ms mw=0.0ms outcome=origin
```

## Table of Contents

**Getting started**: [Introduction](Introduction) · [Quick Start](Quick-Start)

**Using it**: [In-Depth Configuration](In-Depth-Configuration) · [CLI Reference](CLI-Reference) · [HTTP API Reference](HTTP-API-Reference) · [Deployment](Deployment)
**Help**: [Troubleshooting](Troubleshooting) · [FAQ](Frequently-Asked-Questions) · [Security](Security)

**Developers**: [Contributing](Contributing) · [Architecture Internals](Architecture-Internals) · [Testing Guide](Testing-Guide) · [Release Process](Release-Process)
