# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Home Assistant custom component (`ezviz_stream`) that integrates into Ezviz's developer API's to stream from their cloud services.

## Common Commands

Dependencies are managed with **uv**; commands run inside the uv `.venv` via
`uv run`.

### Development

```bash
.devcontainer/scripts/setup    # Bootstrap the container (user setup + uv sync + hooks)
.devcontainer/scripts/develop  # Start a local Home Assistant instance (port 8123)
.devcontainer/scripts/clean    # Reset Home Assistant config directory
uv sync                        # Install/refresh the .venv from pyproject.toml + uv.lock
uv add --dev <pkg>             # Add a dev dependency (updates pyproject.toml + uv.lock)
```

### Linting & Formatting

```bash
.devcontainer/scripts/lint         # uv run ruff format + ruff check --fix
uv run ruff check .                # Lint only (CI uses this)
uv run ruff format . --check       # Format check only (CI uses this)
uv run pre-commit run --all-files  # Run all hooks
```

Pre-commit hooks: pre-commit-hooks (json/yaml/toml validation, hygiene,
detect-private-key), codespell, ruff (+ ruff-format), markdownlint, shellcheck,
actionlint, gitleaks (secret scanning), pip-audit (dependency vulnerabilities),
pytest (full test suite, runs when any `.py` file is staged), duplicate-code
(pylint's copy/paste detector over `custom_components/ezviz_stream`; config in
`pyproject.toml [tool.pylint]`).

### Testing

```bash
uv run pytest tests/                  # Run all tests
uv run pytest tests/test_foo.py       # Run a single test file
uv run pytest tests/test_foo.py::test_bar -v  # Run a single test
```

Tests use `pytest-homeassistant-custom-component` which provides Home Assistant's test infrastructure.

**Code must be accompanied by tests.** Every change to integration source
(`custom_components/ezviz_stream/`) lands with tests that cover it — new behaviour
gets new tests, bug fixes get a regression test. Home Assistant components,
`hass`, config entries, and the like are **mocked out** via
`pytest-homeassistant-custom-component` (its `MockConfigEntry`, `hass` fixture,
and helpers) — never require a live HA instance or real EZVIZ cloud calls in a
test. The `pytest` pre-commit hook runs the full suite whenever a `.py` file is
staged, so tests must pass before a commit completes.

## Architecture

**The authoritative design is `doc/specification.md`** — read it before working on
the stream pipeline. **`doc/TODO.md` is the project's official todo list** — the
source of truth for next actions and build milestones; keep it current as work
lands. Summary of the proven pipeline and the decisions taken so far:

```
EZVIZ cloud login → device list → VTDU tokens        # control plane (auth)
  → VTM/VTDU binary handshake (ysproto://)           # obtain a media socket
  → channel-0x01 RTP packets → RTP/RFC-7798 depacketize → Annex-B HEVC
  → FFmpeg (default: HEVC→H.264 transcode) → HA camera
```

- **Auth layer:** depend on `RenierM26/pyEzvizApi` (login / device list / tokens);
  implement only the VTM/VTDU socket handshake ourselves.
- **De-packetizer:** the RTP→HEVC logic in spec §4.1 is proven working — port it
  verbatim; it is the core contribution.
- **Codec:** default to on-demand HEVC→H.264 transcode (works in all browsers);
  native HEVC as a config option (Safari/iOS only).
- **Serving path:** decided — go2rtc `exec:` source (no separate transcoding
  container; go2rtc gives on-demand start/stop + fan-out). Standalone add-on kept
  as a documented fallback only.
- **Coupling to official `ezviz` integration:** soft, not a dependency — own config
  flow + creds; camera device-linked to the same device via a matching
  `device_info` identifier (Powercalc-style).
- **Battery-friendly:** stream **only** while a client is watching; never 24/7.
  Handle cam-wake retry (first request often returns 0 packets) and the ~27 s VTDU
  drop with a reconnect loop.

## Dependencies

- **Dev/tooling deps** live in `pyproject.toml` (`[dependency-groups] dev`), locked
  in `uv.lock`, installed into `.venv` by `uv sync`. There is **no
  `requirements.txt`** — pyproject + uv.lock are the single source of truth.
- **Runtime deps of the integration** go in
  `custom_components/ezviz_stream/manifest.json` (the `requirements` key). Home
  Assistant reads that and pip-installs those packages **at launch**, into the
  running HA environment — they are *not* installed by `uv sync`.
- **Mirror manifest.json requirements into `[project].dependencies`.** Because HA
  only installs them at runtime, our tooling (ruff, mypy, pytest, Pylance) can't
  see them otherwise. Whenever you add/bump a requirement in `manifest.json` (e.g.
  `pyEzvizApi`), add the matching entry to `[project].dependencies` in
  `pyproject.toml` and run `uv sync`. Keep dev/test tooling (HA itself, ruff,
  pytest, …) in the `dev` group — **not** in `[project].dependencies`.
- **Dependency vuln scanning is scoped to our tree.** The `pip-audit` pre-commit
  hook audits `uv export --no-dev` — i.e. `[project].dependencies` only. HA's large
  transitive tree (the `dev` group) is deliberately excluded, since we don't
  control it. This is why runtime deps must go in `[project].dependencies`.
- **Dependabot** (`.github/dependabot.yml`, `uv` ecosystem) updates dependencies but
  **excludes `homeassistant`** — its version tracks the `hacs.json` floor, so a
  Dependabot bump would break that.

## Dev Environment

The container splits setup by scope, so the frequently-run `setup` stays fast:

- **`.devcontainer/Dockerfile`** — global, machine-wide installs (system apt
  packages, standalone tool binaries: gh, gitleaks, actionlint, uv). Baked into
  the cached image; changes rarely.
- **`.devcontainer/scripts/setup`** — user-specific and project setup (shell
  config, Claude Code, `uv sync`, pre-commit hooks). Runs on every container
  (re)create via `postCreateCommand`.

If you need a tool that isn't installed, install it **and persist it**: global
tools go in the Dockerfile, user/project tools go in the `setup` script — never
rely on an ad-hoc install that vanishes on the next rebuild.

## MCP Launchpad (`mcpl`)

`mcpl` is a CLI gateway to tools from all configured MCP servers. The user's MCP
config can change at any time, so **when a task needs a capability outside the
current tools, check `mcpl` first.**

- **Always discover before calling — never guess tool names** (they vary by
  server). Search, then call:

  ```bash
  mcpl search "<query>"            # find tools across servers (shows required params)
  mcpl list <server>              # list a server's tools
  mcpl inspect <server> <tool> --example   # full schema + ready-to-use example call
  mcpl call <server> <tool> '{"param": "value"}'   # execute
  mcpl verify                     # test all server connections
  ```

## Ruff Configuration

All lint rules enabled (`select = ["ALL"]`) with specific exclusions. Target: Python 3.14. Max complexity: 25. Test files have relaxed rules (asserts, magic values, missing docstrings allowed). See `.ruff.toml` for details.

- **Prefer inline suppression over global suppression.** To silence a lint rule in
  integration code (`custom_components/`), use a targeted inline `# noqa: <CODE>`
  (with a reason) on the offending line — do **not** add a rule to `ignore` or
  `per-file-ignores` in `.ruff.toml`. The exceptions are the `tests/` and
  `scripts/` directories, whose `per-file-ignores` blocks in `.ruff.toml` are the
  accepted place to relax rules wholesale (test/CLI ergonomics), **and the low-level
  protocol/codec modules** (`custom_components/ezviz_stream/{decrypt,ysproto}.py`),
  which have a scoped `per-file-ignores` block relaxing magic-value/complexity/
  message-style rules — dense bit-twiddling (NAL masks, RTP fields, AES maths) where
  naming every mask hurts readability. Keep that carve-out *narrow* (only those
  files); all other integration code stays under full linting with inline `noqa`.

## CI/CD

Two GitHub Actions workflows on push/PR to main:

- **lint.yml** — `ruff check` and `ruff format --check`
- **validate.yml** — Home Assistant `hassfest` manifest validation and HACS validation

## Branching and releases

Since v0.1.0 the repo uses a two-branch model:

- **`main` is protected** — no direct pushes (enforced for admins too); every change
  lands via a **pull request**. Self-merge is allowed (0 required approvals), so a
  solo maintainer opens a PR and merges it once CI is green.
- **`develop`** is the day-to-day integration branch. Do work on `develop` (or feature
  branches off it) and open the PR into `main`.

**Release process** (as run for v0.1.0):

1. Land everything for the release on `main` via PR, bumping the `version` in
   `custom_components/ezviz_stream/manifest.json` in that PR.
2. From `main` (green CI, clean tree), tag it:
   `git tag -a vX.Y.Z -m "EZVIZ Stream vX.Y.Z" && git push origin vX.Y.Z`.
3. Publish: `gh release create vX.Y.Z --title "vX.Y.Z" --notes-file <notes> --latest`.
4. HACS installs the **latest GitHub release**, so real releases are `--latest` (use
   `--prerelease` only for betas). Do **not** bump `homeassistant` in `hacs.json` /
   Dependabot for a routine release — it is the support floor, not the target.

## Working Conventions

- **Never auto-commit or push** — always ask first, **every time**. A prior
  approval to commit/push does **not** carry forward to later commits; re-ask for
  each one, even mid-session.
- **Don't branch automatically** — the user handles branching. Day-to-day work lands
  on `develop`; `main` is protected and reached only via PR (see Branching and
  releases).
- **No self-attribution** — do not add "Authored by / Generated with Claude Code"
  or `Co-Authored-By` lines to commits, PRs, or any artifact.
- **When you do commit, commit all changed and new files** (`git add -A`). Never
  create partial commits — every commit includes the complete set of changes.
- **Before finalizing a commit, check for secrets and accidental files** — scan
  the staged diff for credentials/keys and for anything that shouldn't be
  committed (`.venv`, config artifacts, scratch files) and stop if found.
- **Don't let issues hang.** Surface problems proactively; fix low-impact ones
  directly, ask before fixing high-impact ones. Never bypass failing checks,
  broken tests, or other issues just to keep going.
- **Code must be accompanied by tests.** New/changed integration code lands with
  tests; mock Home Assistant with `pytest-homeassistant-custom-component` (see
  Testing above). The `pytest` pre-commit hook enforces this on every `.py` change.
- **Debugging / diagnostic tools go in `scripts/`, never in
  `custom_components/ezviz_stream/`.** The component package ships to users and stays
  runtime-only; standalone CLIs, live-verification probes, and one-off debugging
  helpers live in `scripts/` (see `scripts/README.md`), where `.ruff.toml` relaxes
  CLI-ergonomics rules and the `.env`/`--creds-file` credential pattern lives. Reusable
  logic a script needs stays in the component (and is tested there); the script only
  imports it. They add the repo root to `sys.path` to import
  `custom_components.ezviz_stream.*`.
- **Research, don't assume** — verify options (including via web search) rather
  than assuming APIs/libraries behave as described.
- **If something can be caught by a pre-commit hook, add it** — prefer enforcing a
  rule mechanically over relying on memory.
- **No em-dashes.** Do not use em-dashes (`—`) in prose, docs, comments, or any
  authored text. Use a plain hyphen (`-`) instead, or restructure the sentence.
