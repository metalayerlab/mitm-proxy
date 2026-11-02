# 0008: Adopt interpose for settings and secrets

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Proposed
Date: 2026-11-02
Authors: Stephen Cox; drafted by Claude (Claude Code)

## Context

`config.py` holds mitm-proxy's own copy of the template's settings engine: the `_FILE` > env > YAML >
default lookup, coercion, and its `Setting` type. sluice and the template hold two more copies, and the
three had drifted (interpose design 0001). mitm-proxy's copy has two silent bugs: a YAML float for an
int setting is truncated (`max_buffer_bytes: 0.5` became `0`), and a YAML list or mapping for a str
setting becomes its Python repr. Its secrets are plain `str` fields kept out of `repr()` by hand
(`field(repr=False)`), so any other path (an f-string, a log call, an exception) would show them.

[interpose](https://github.com/metalayerlab/interpose) v0.1.0 is the shared engine: `Setting`,
`Settings`, strict coercion, per-setting `check`, and `Secret`. Its design 0001 asks each consumer to
switch with a record of its own, because it adds a dependency.

Constraints:

- Every config key and environment variable stays as it is. `server.py`, the CLI and
  `scripts/gen-wiki.py` iterate `SETTINGS` and read `key`, `default`, `help`, `secret`, `is_list` and
  `env`; that keeps working.
- `config.ConfigError`, `config.SETTINGS` and `config.CONFIG_ENV` stay importable from `config.py`.
- A template-python record will later generate `config.py` on interpose, so its shape must match.
- Out of scope: configuring middleware, and metrics (interpose 0002 and 0003 come later).

## Decision

1. **Dependency:** `interpose @ git+https://github.com/metalayerlab/interpose@v0.1.0`, a pinned tag,
   never PyPI, first in `dependencies`. hatchling needs `[tool.hatch.metadata]
   allow-direct-references = true` for that. Both follow template-python's decision 0003 to the letter,
   so its update merges cleanly; `pyyaml` and `types-pyyaml` stay for the same reason. The image's
   builder stage needs `git` to fetch the tag: `build: git` in the project-owned
   `contrib/system-packages.txt` (the Dockerfile is template-owned). `dependabot.yml` is template-owned
   too, so ignoring `interpose` there comes with the template update.
2. **`config.py`'s shape:**
   - one `SETTINGS = Settings("MITM_PROXY_", (...), default_path=Path("/etc/mitm-proxy/config.yaml"))`,
     the only place `Settings` is constructed, and `CONFIG_ENV = SETTINGS.config_env`;
   - rules about one setting are its `check=`: `one_of(*LOG_LEVELS)`, `port`, `positive`, and local
     checks for `intercept.hosts` (allowlist patterns), `middleware.available` (`name=module:attribute`,
     each name once), `middleware.chain` (no repeats) and `proxy.auth` (`user:password`);
   - rules that span settings stay in `load_config`: every `middleware.chain` name must be in
     `middleware.available` (and the old `log` gets its own message), and `ca.cert` and `ca.key` go
     together;
   - `ConfigError`, `Value` and `Secret` are re-exported; `Config` and `load_config` keep their API.
     `log_level` is still upper-cased in `load_config` (`one_of` keeps the case it was given).
3. **Secrets:** `proxy.auth` and `ca.key` resolve to `Secret`, and `Config.proxy_auth` and
   `Config.ca_key` are typed `Secret` (`load_config` narrows `Resolved.values` with `cast`, never
   `str()`, which gives the mask). The places that need the value call `reveal()`: the proxy's
   authentication callback and `load_root(key_pem=...)` (in `server.py` and the CLI's `ca show`).
   `config_hash` still records only set or unset, and `mitm-proxy config` still prints `********`.
   A check's reason has a secret's value masked by interpose, and a type error for a secret names only
   the key and type.
4. **Tests:** the engine tests interpose now covers go (coercion, `_FILE` reading, YAML errors). The
   tests of mitm-proxy's names, checks and cross-setting rules stay, with new ones for the unchanged
   key and variable list, the stricter coercion, and secrets reaching neither errors, `repr()`, the
   status, nor `/metrics`.

### Behaviour that becomes stricter (on purpose)

| Input | Before | Now |
|---|---|---|
| YAML float for an int setting (`port: 3128.0`, `max_buffer_bytes: 0.5`) | truncated | `ConfigError: <key>: expected int` |
| YAML list or mapping for a str setting (`listen.host: [a, b]`) | its repr | `ConfigError: <key>: expected str` |
| An int with underscores from the environment or a `_FILE` (`1_000`) | `1000` | `ConfigError` (a YAML `1_000` is still `1000`) |
| A `_FILE` value's leading whitespace | stripped | kept (trailing whitespace is still stripped) |
| An invalid YAML file | the message quoted the line | line and column only (the line may hold a secret) |

A config that was silently misread now fails at startup with one line naming the key, which is the
point. A type error also says where the value came from (`(from MITM_PROXY_LISTEN_PORT)`), and when
several values are invalid, the first in declaration order is reported.

## Alternatives considered

- **Keep the copy and fix its two bugs by hand:** fixes today's drift, not the next one, and leaves
  `Secret` to be written again for the middleware configuration that mitm-proxy still lacks.
- **Depend through `[tool.uv.sources]` (what `uv add` writes by default):** the published metadata would
  then say only `interpose`, which pip resolves from PyPI, where the name isn't ours. The PEP 508 URL
  keeps the tag in the package metadata.
- **Keep `str` fields for the secrets and wrap them only at display time:** every new use site would
  have to remember; the type is what makes a leak a visible `********` instead.
- **Move every rule into `check=`:** the chain-in-registry and cert-with-key rules need two settings;
  `check` sees one value (interpose 0001 keeps those in the project).

## Consequences

- One engine for mitm-proxy, sluice and (later) the template; an engine fix arrives as an interpose tag
  bump in a reviewed PR.
- Dependabot can't follow a git tag (interpose 0001, question 4), so bumps are by hand until the
  template's tag-check workflow arrives (owned by template-python, not added here).
- Middleware configuration (`read_secret_dir`, sluice's `env.expose` model) can build on `Secret` later.
- Rollback: revert the PR. No key or variable changed, so deployments need nothing.
- Wiki: In-Depth-Configuration (types and strict coercion), Troubleshooting (the new errors),
  Architecture-Internals (`config.py`'s role), Security (`Secret`).

## Open questions

1. **Status:** this record is written and implemented in the same PR. Recommend: approving the PR
   approves the record, and it moves to Implemented before merge.
