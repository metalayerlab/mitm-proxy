# CLI Reference

`mitm-proxy --help` lists the commands. `mitm-proxy <command> --help` shows a command's options.

## Commands

Generated from the code: don't edit, run `uv run scripts/gen-wiki.py`.

<!-- generated:start cli -->
### `mitm-proxy ca`

The root CA that signs intercepted hosts' certificates.

### `mitm-proxy config`

Print the effective configuration and where each value came from. Secrets are masked.

| Option | Environment variable | Description |
|---|---|---|
| `--config`, `-c` | `MITM_PROXY_CONFIG` | Config file (default /etc/mitm-proxy/config.yaml; missing is fine). |

### `mitm-proxy scope`

The intercept allowlist.

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

<!-- guidance: add project-specific exit codes, and worked examples of common invocations. -->
