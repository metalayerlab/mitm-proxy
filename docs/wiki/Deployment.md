# Deployment

Manifests live in [`contrib/`](https://github.com/metalayerlab/mitm-proxy/tree/main/contrib), versioned with the code. release-please moves their image tag to each new release. Every option keeps the same hardening: a non-root user, read-only root, no capabilities.

## Docker Compose

```sh
mkdir -p config state
cp contrib/mitm-proxy.example.yaml config/config.yaml   # edit it
docker compose -f contrib/docker/compose.yml up -d
docker compose -f contrib/docker/compose.yml logs -f
```

Two ports are published, both on `127.0.0.1` only: the admin API (8080) and the proxy (3128). Inside the container both listeners bind `0.0.0.0`: the image sets `MITM_PROXY_LISTEN_HOST` and `MITM_PROXY_PROXY_HOST` (the compose file repeats them, to be explicit), because the `127.0.0.1` default outside containers can't be reached through a published port. The published ports decide what's reachable. Because of that, the proxy's startup warning about listening on `0.0.0.0` without `proxy.auth` also appears in containers; it matters only if you publish 3128 beyond loopback. To offer the proxy to other machines, publish 3128 beyond loopback **and** set `proxy.auth` (`MITM_PROXY_PROXY_AUTH_FILE`). Put a reverse proxy (Traefik) in front of the admin API for remote access.
Secrets go in files mounted read-only, passed as `MITM_PROXY_<KEY>_FILE`. The root CA is generated in `./state/ca/` on first start: keep that directory, it's what your clients trust.

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

The Service exposes the admin API (`http`, 8080) and the proxy (`proxy`, 3128). The NetworkPolicy admits only the `traefik` namespace to the admin API and **no one** to the proxy port: add a rule for each namespace whose pods should use the proxy (an example is commented in `networkpolicy.yaml`), and set `proxy.auth`.

State is on an `emptyDir`, so the root CA is regenerated whenever the pod is replaced, and clients must trust the new one each time. For a stable CA, swap it for a PersistentVolumeClaim, or supply the CA from a Secret: `deployment.yaml` has the `secrets` volume and the `MITM_PROXY_CA_CERT_FILE`, `MITM_PROXY_CA_KEY_FILE` and `MITM_PROXY_PROXY_AUTH_FILE` variables commented out, ready to enable. Secrets: SOPS-encrypted Secrets in the GitOps repo, mounted as files.

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

The proxy listens on `127.0.0.1:3128` and the admin API on `127.0.0.1:8080` (set `proxy.host` and `proxy.auth` to serve other machines). The root CA is generated in `/var/lib/mitm-proxy/ca/` on first start. The unit runs as the `mitm-proxy` system user in a tight sandbox (`systemd-analyze security mitm-proxy` scores it). If a feature needs more access, loosen exactly one directive, with a comment saying why.

## Upgrading

Read the [CHANGELOG](https://github.com/metalayerlab/mitm-proxy/blob/main/CHANGELOG.md) entries between your version and the new one; breaking changes are marked ⚠. Then:

- Compose: change the image tag, then `docker compose up -d`.
- Kubernetes: change `?ref=` (or the `images:` tag) in the overlay, and let Flux reconcile.
- systemd: `uv pip install --python /opt/mitm-proxy git+https://github.com/metalayerlab/mitm-proxy@vX.Y.Z`, then `systemctl restart mitm-proxy`.

**Keep `<state_dir>/ca/` across upgrades:** it's the root CA your clients trust, and the file layout there is fixed. A new release never needs a data migration so far. New settings arrive with defaults, so an existing config file keeps working.

To roll back, deploy the previous image tag (Compose, Kubernetes) or reinstall the previous version (systemd). The CA and config files work with either.
