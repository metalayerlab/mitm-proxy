# Troubleshooting

## Diagnostics

- **Effective configuration:** `mitm-proxy config` prints every setting and where it came from (default, file, env var). Secrets are masked.
- **More logging:** `MITM_PROXY_LOG_LEVEL=DEBUG`.
- **Health:** `curl http://127.0.0.1:8080/healthz` (alive) and `/readyz` (ready to serve).
- **Container:** `docker logs mitm-proxy`, and `docker inspect --format '{{json .State.Health}}' mitm-proxy`.
- **Kubernetes:** `kubectl -n mitm-proxy logs deploy/mitm-proxy` and `kubectl -n mitm-proxy describe pod`.
- **systemd:** `journalctl -u mitm-proxy -e` and `systemctl status mitm-proxy`.

## Common problems

<!-- guidance: one ### heading per symptom, in the user's words ("Exits with 'cannot read ..._FILE'"), then the cause and the fix. Add one whenever a bug report or a support question shows a new failure mode. -->

### `error: cannot read MITM_PROXY_..._FILE`

The file named by a `_FILE` variable doesn't exist, or the process can't read it. Check the path inside the container or unit, and the file's owner and mode: the process runs as uid 1000 / the `mitm-proxy` user.
