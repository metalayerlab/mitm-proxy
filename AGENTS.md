# AGENTS.md

mitm-proxy: Async Python MITM proxy with a self-signed CA — HTTP/1.1 & HTTP/2, pluggable middleware for inspecting and rewriting traffic in flight.
Single source of instructions for AI assistants (CLAUDE.md, GEMINI.md, Copilot and Cursor files point here).
The reasons behind these rules are in the wiki (`docs/wiki/Contributing.md`); you don't need them to follow them.

## How to work

1. Read only what the task needs: the page for your area in the [Docs map](#docs-map),
   `docs/design/README.md` (the index; then only the records it points you to), and `TODO.md` "Decided against"
   (never re-propose those).
2. New feature, new dependency, or a public-interface change (config keys, CLI, HTTP API, file formats):
   write `docs/design/NNNN-*.md` from `0000-template.md`, add a line to its index, and **stop for approval**.
3. One concern per PR. A human reviews every line.
4. Done = code + tests + wiki ([Definition of done](#definition-of-done)).
5. Report what you skipped or couldn't run. Never claim an unrun check passed.

## Don't read

These are large or generated, and reading them wastes context. Use the commands instead:

- `uv.lock` (use `uv tree --depth 1`), `CHANGELOG.md` (use `git log`), `.venv/`, `htmlcov/`, `.coverage`, `.mypy_cache/`, `.ruff_cache/`, `.pytest_cache/`, `.graphify/`
- Generated wiki blocks (`<!-- generated:start/end -->`): the code is the source.
- `docs/design/` records the index doesn't point you to.

Adding a large or generated path? List it here and deny `Read` on it in `.claude/settings.json`.

## Stack and commands

Python >= 3.13 with uv. Always `uv run`; never `pip install` or activate the venv.

| Task | Command |
|---|---|
| Install | `uv sync` |
| Add a dependency | `uv add <pkg>` (dev: `uv add --dev <pkg>`) |
| Lint (fix) / format | `uv run ruff check --fix` / `uv run ruff format` |
| Type check (strict) | `uv run mypy` |
| Tests (80% floor) / integration | `uv run pytest` / `RUN_INTEGRATION=1 uv run pytest -m integration` |
| Regenerate wiki sections | `uv run scripts/gen-wiki.py` |
| Check the wiki | `python3 scripts/check-wiki.py` |
| Check manifests | `scripts/check-manifests.sh` |
| Run | `uv run mitm-proxy --help` |

## Repository map

T = template-owned: never edit here. Changes go to [project-template](https://github.com/metalayerlab/project-template) and arrive by the weekly update PR.

| Path | What |
|---|---|
| `src/mitm_proxy/` | The package: `config.py`, `cli.py` (Typer), `api.py` (FastAPI) |
| `tests/unit/`, `tests/integration/` | pytest; integration is marked and opt-in |
| `docs/wiki/` | The wiki (published from `main`); `.standard.toml` = required pages (T) |
| `docs/design/` | Design records; `README.md` is the index |
| `docs/ai/` | Prompts, review checklist (T) |
| `contrib/` | Manifests: docker, k8s, systemd |
| `Dockerfile` | Image (T skeleton) |
| `.github/workflows/`, `scripts/` | CI and its scripts (T) |
| `CHANGELOG.md`, version in `pyproject.toml`, `.release-please-manifest.json` | release-please only |
| `.copier-answers.yml` | Copier only |

## Conventions

- **Types:** full annotations; `mypy --strict` passes. Any `# type: ignore` states its reason.
- **Style:** ruff decides (line length 100). Comments say *why*.
- **Output:** `logging` in library code; only the CLI writes stdout (`typer.echo`). No `print` (ruff T20).
- **Config:** every setting goes through `config.SETTINGS`: `MITM_PROXY_<KEY>_FILE` > `MITM_PROXY_<KEY>` > file > default. Secrets: `secret=True`, read from `_FILE`, never logged.
- **Errors:** specific exceptions (`ConfigError`, ...); the CLI prints one line and exits 2. No bare `except:`.
- **Dependencies:** only when the stdlib or an existing dependency can't do it, justified in the PR. Check the exact package name exists (typosquatting).
- **Tests:** each behaviour change gets a test that fails without it. Assert behaviour; no network in unit tests.
- **PR titles:** Conventional Commits (`feat:` `fix:` `docs:` `refactor:` `perf:` `test:` `build:` `ci:` `chore:`, `!` = breaking), lower-case subject, no full stop. The title becomes the CHANGELOG line.
- **Branches:** `feat/<topic>` or `dev/<topic>`.

## Never

- Push to `main`, create tags or releases, or edit release-please or Copier files (see the map).
- Edit inside generated wiki blocks: regenerate them.
- Commit secrets, real hostnames, IPs or personal data. Use `example.org` and `192.0.2.0/24`.
- Weaken a check: lower the coverage floor, add an unexplained `noqa`/`type: ignore`, skip or xfail a test, loosen the sandbox or `securityContext`, add `continue-on-error`. If a check is wrong, say so.
- Add the `no-docs-needed` label (reviewers only), telemetry, or network calls at import time.
- Grow this file or the tool pointer files past `[context_budget]` (`docs/wiki/.standard.toml`, CI-checked): add rules tersely, put reasons in the wiki.

## Definition of done

```sh
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest
uv run scripts/gen-wiki.py --check
python3 scripts/check-wiki.py
scripts/check-manifests.sh
```

All of these pass, and the [Docs map](#docs-map) pages for your change are updated in the same PR.

## Docs map

CI fails a PR that changes watched code without touching `docs/wiki/`.

| You changed | Update |
|---|---|
| a setting (`config.py`) | In-Depth-Configuration (prose; the table is generated) |
| a command or option (`cli.py`) | CLI-Reference (regenerate), Quick-Start if shown |
| a route (`api.py`) | HTTP-API-Reference: regenerate, then document request, response, errors |
| `Dockerfile`, `contrib/**` | Deployment |
| component structure | Architecture-Internals + a design record |
| auth, secrets, input handling, exposure | Security |
| a new failure mode or error | Troubleshooting |
| test layout or tooling | Testing-Guide |
| a recurring question | Frequently-Asked-Questions |

Wiki links use page names (`[Quick Start](Quick-Start)`), never repo paths. Link new pages from `_Sidebar.md`.
