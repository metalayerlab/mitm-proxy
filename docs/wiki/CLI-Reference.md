# CLI Reference

`mitm-proxy --help` lists the commands. `mitm-proxy <command> --help` shows a command's options.

## Commands

Generated from the code: don't edit, run `uv run scripts/gen-wiki.py`.

<!-- generated:start cli -->
### `mitm-proxy ca init`

Create the root CA in <state_dir>/ca/ and print its SHA-256 fingerprint.

| Option | Environment variable | Description |
|---|---|---|
| `--config`, `-c` | `MITM_PROXY_CONFIG` | Config file (default /etc/mitm-proxy/config.yaml; missing is fine). |
| `--force` |  | Replace an existing CA. Clients must trust the new one. |

### `mitm-proxy ca show`

Write the root CA certificate to stdout, to install on clients.

| Option | Environment variable | Description |
|---|---|---|
| `--config`, `-c` | `MITM_PROXY_CONFIG` | Config file (default /etc/mitm-proxy/config.yaml; missing is fine). |
| `--der` |  | DER instead of PEM. |

### `mitm-proxy config`

Print the effective configuration and where each value came from. Secrets are masked.

| Option | Environment variable | Description |
|---|---|---|
| `--config`, `-c` | `MITM_PROXY_CONFIG` | Config file (default /etc/mitm-proxy/config.yaml; missing is fine). |

### `mitm-proxy scope check`

Say whether HOST would be intercepted, and which intercept.hosts entry matched.

Exit code 0: intercepted. 1: tunnelled untouched. 2: an error.

| Argument | Description |
|---|---|
| `HOST` | A hostname or IP address. |

| Option | Environment variable | Description |
|---|---|---|
| `--config`, `-c` | `MITM_PROXY_CONFIG` | Config file (default /etc/mitm-proxy/config.yaml; missing is fine). |

### `mitm-proxy serve`

Run the proxy and the admin HTTP API until SIGTERM or Ctrl-C.

Loads the root CA (generating it on first start) and imports the middleware chain.

| Option | Environment variable | Description |
|---|---|---|
| `--config`, `-c` | `MITM_PROXY_CONFIG` | Config file (default /etc/mitm-proxy/config.yaml; missing is fine). |

### `mitm-proxy version`

Print the version.
<!-- generated:end cli -->

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Failure while running |
| 2 | Bad usage or invalid configuration |

`mitm-proxy scope check` also uses 1: the host would be tunnelled, not intercepted.

### Examples

```sh
mitm-proxy ca init                          # before first start, to install the CA ahead of time
mitm-proxy ca show > mitm-ca.pem            # the certificate for clients
mitm-proxy ca show --der > mitm-ca.cer      # for phones and Windows
mitm-proxy scope check www.example.org      # www.example.org: intercepted (matches *.example.org)
MITM_PROXY_LOG_LEVEL=DEBUG mitm-proxy serve -c ./config.yaml
```

`serve` exits 2 with one line on stderr when it can't start: an invalid setting, an unreadable CA, a port in use, or middleware that won't load.
