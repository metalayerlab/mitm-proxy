# Deployment

Manifests live in [`contrib/`](https://github.com/metalayerlab/mitm-proxy/tree/main/contrib), versioned with the code. release-please moves their image tag to each new release. Every option keeps the same hardening: a non-root user, read-only root, no capabilities.

## Docker Compose

```sh
mkdir -p config state
cp contrib/mitm-proxy.example.yaml config/config.yaml   # edit it
docker compose -f contrib/docker/compose.yml up -d
docker compose -f contrib/docker/compose.yml logs -f
```

The port binds to `127.0.0.1` only: put a reverse proxy (Traefik) in front for TLS and remote access.
Secrets go in files mounted read-only, passed as `MITM_PROXY_<KEY>_FILE`.

## Kubernetes

[`contrib/k8s`](https://github.com/metalayerlab/mitm-proxy/tree/main/contrib/k8s) is a kustomize base. It contains a Namespace with the *restricted* Pod Security Standard, a Deployment, a Service, a NetworkPolicy, and a ConfigMap generated from `config.yaml`.

```sh
kubectl apply -k contrib/k8s                       # directly
```

With Flux, point an overlay in the cluster repo at a release:

```yaml
# kustomization.yaml in the GitOps repo
resources:
  - https://github.com/metalayerlab/mitm-proxy//contrib/k8s?ref=v1.2.3
patches: []          # replicas, resources, the ConfigMap's config.yaml, ingress
```

The NetworkPolicy admits only the `traefik` namespace: add each in-cluster client. State is on an `emptyDir`; swap it for a PersistentVolumeClaim if it must survive a restart. Secrets: SOPS-encrypted Secrets in the GitOps repo, mounted as files.

## systemd

```sh
install -Dm644 contrib/systemd/mitm-proxy.sysusers /usr/lib/sysusers.d/mitm-proxy.conf
systemd-sysusers
UV_PYTHON_INSTALL_DIR=/opt/uv-python uv venv /opt/mitm-proxy
uv pip install --python /opt/mitm-proxy git+https://github.com/metalayerlab/mitm-proxy@v1.2.3
install -Dm644 contrib/mitm-proxy.example.yaml /etc/mitm-proxy/config.yaml   # edit it
install -Dm644 contrib/systemd/mitm-proxy.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now mitm-proxy
journalctl -u mitm-proxy -f
```

The unit runs as the `mitm-proxy` system user in a tight sandbox (`systemd-analyze security mitm-proxy` scores it). If a feature needs more access, loosen exactly one directive, with a comment saying why.

## Upgrading

Read the [CHANGELOG](https://github.com/metalayerlab/mitm-proxy/blob/main/CHANGELOG.md) entries between your version and the new one; breaking changes are marked ⚠. Then:

- Compose: change the image tag, then `docker compose up -d`.
- Kubernetes: change `?ref=` (or the `images:` tag) in the overlay, and let Flux reconcile.
- systemd: `uv pip install --python /opt/mitm-proxy git+https://github.com/metalayerlab/mitm-proxy@vX.Y.Z`, then `systemctl restart mitm-proxy`.

<!-- guidance: data or config migrations a release needs, and how to roll back. -->
