# Quick Start

From nothing to a working mitm-proxy in a few minutes. Everything else is in [In-Depth Configuration](In-Depth-Configuration) and [Deployment](Deployment).

## Prerequisites

- Docker (or any OCI runtime)
- For running from source: Python >= 3.13 and [uv](https://docs.astral.sh/uv/)
<!-- guidance: add what it talks to: accounts, API keys, other services. -->

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

<!-- guidance: extend this into the first real task a user does, with the expected output. -->

## Next steps

- [In-Depth Configuration](In-Depth-Configuration): every setting
- [Deployment](Deployment): docker, k8s, systemd
- [Troubleshooting](Troubleshooting) if something doesn't work
