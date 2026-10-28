# Quick Start

From nothing to a working mitm-proxy in a few minutes. Everything else is in [In-Depth Configuration](In-Depth-Configuration) and [Deployment](Deployment).

## Prerequisites

- Docker (or any OCI runtime)
- For running from source: Python >= 3.13 and [uv](https://docs.astral.sh/uv/)
- A client you control (a browser, `curl`) that can use an HTTP proxy, and where you can install a root CA
- Nothing else: mitm-proxy talks only to the hosts your clients ask for

## Install

```sh
docker pull ghcr.io/metalayerlab/mitm-proxy:latest
```

```sh
uv tool install git+https://github.com/metalayerlab/mitm-proxy
```

## First run

```sh
docker run -d --name mitm-proxy -p 127.0.0.1:8080:8080 ghcr.io/metalayerlab/mitm-proxy:latest
curl http://127.0.0.1:8080/healthz      # {"status":"ok"}
docker exec mitm-proxy mitm-proxy config   # the effective configuration
```

In a container, run it like this. The image binds both listeners to all interfaces inside the container (the host default is `127.0.0.1`), so the published ports decide what's reachable: here, loopback only.

```sh
docker run -d --name mitm-proxy \
  -p 127.0.0.1:8080:8080 -p 127.0.0.1:3128:3128 \
  -e MITM_PROXY_INTERCEPT_HOSTS=example.org \
  -e MITM_PROXY_PROXY_ACCESS_LOG=true \
  ghcr.io/metalayerlab/mitm-proxy:latest
```

Then look inside a request to `example.org`:

```sh
curl -s http://127.0.0.1:8080/ca.pem -o mitm-ca.pem      # 1. get the root CA
curl --proxy http://127.0.0.1:3128 --cacert mitm-ca.pem https://example.org/   # 2. use it
docker logs mitm-proxy | grep mitm_proxy.flows          # 3. see the flow
```

```text
INFO mitm_proxy.flows: 3f2c… http/1.1>h2 GET https://example.org/ 200 req=0 resp=1256 total=41.3ms connect=0.0ms(reused) ttfb=29.1ms mw=0.0ms outcome=origin
```

Without `--cacert`, curl refuses the proxy's certificate: that's the check working. A browser needs the CA installed in its trust store; download it from `http://127.0.0.1:8080/ca.pem` (or `/ca.der` on phones). Hosts you didn't list still work, untouched: try `https://example.net/` through the same proxy.

The CA is generated on first start in `/var/lib/mitm-proxy/ca/`. Mount a volume there (`-v ./state:/var/lib/mitm-proxy`) to keep it, or you'll have a new one to trust after every new container.

## Next steps

- [In-Depth Configuration](In-Depth-Configuration): every setting
- [Deployment](Deployment): docker, k8s, systemd
- [Troubleshooting](Troubleshooting) if something doesn't work
