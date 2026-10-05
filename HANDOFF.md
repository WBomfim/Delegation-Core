# delegation-core: Project Handoff

_Last updated: 2026-10-03 (core v0.15.0). The sections "What this is", "Repos and
remotes", "Current state" and "Architecture notes" were re-verified on that date
against the working tree and the running daemon. Older sections say their own date._

Written for whoever (human or agent) picks this project up next. Re-verify anything
load-bearing before acting on it.

## What this is

A local MCP server (FastMCP over HTTP, one daemon per machine) over a Markdown vault
(ChromaDB + BGE embeddings), with an optional local model (llama.cpp, or any server
that answers `/health` and `/v1/chat/completions`), a vendored code-graph pipeline
(from Graphify), a full CLI, and a cross-platform Tauri desktop dashboard. For the
live tool list, call `capabilities()`: it asks the running server rather than
repeating a number here. `docs/MAPA.md` is the structural map.

## Repos and remotes

- Working checkout: `/home/joey/Projects/delegation-core`. On this machine the
  installed package is an editable install of this checkout, so what `master` has
  is what the daemon runs after a restart.
- **Active repo: `origin` → github.com/AnonJoey/Delegation-Core-Office** (public,
  default branch `master`). The older name `delegation-core-v7` redirects here.
- Frozen archive: `origin-v6.4-obsoleto` → github.com/AnonJoey/delegation-core-v6.4.
  Kept only so old URLs resolve; do not push.
- `fork` → the local clone `~/Projects/delegation-core-TEST`, a test fork whose `main`
  carries an unmerged "enrichment" line of work (see its `MERGE-ENRIQUECIMENTO.md`).
  It diverged before PRs 7 to 15 and no longer merges cleanly.
- Team members contribute through their own forks and pull requests.

## Current state (all verified, not assumed)

- **Tests: run them, do not read a number here.**
  `~/.delegation_core/venv/bin/python -m pytest -q`. Fast and offline (fakes and
  monkeypatch; no real BGE, ChromaDB, model or network), and `tests/conftest.py`
  redirects every state path so a run cannot touch `~/.delegation_core`. Check the
  exit code of pytest itself, not of a pipe after it.
- **Static analysis is part of the suite.** `tests/test_analise_estatica.py` runs ruff
  (pyflakes and syntax errors) over `src/`, hooks included; `pip install -e .[dev]` brings
  ruff, and without it the test is skipped. It exists because an undefined `{_lang}`
  broke local `compress` for a month with every test green.
- **CI runs the suite on every push and PR to `master`**, on Linux, Windows and
  macOS (`.github/workflows/testes.yml`). Its first Windows run found 35 failures,
  most of them real Windows defects (backslash note paths); keep all three green.
  The dashboard build (`build-dashboard.yml`) still runs only on `dashboard-v*`
  tags or by hand.
- **Session hooks run from the package.** `delegation-core-hook session-start` and
  `session-end`, registered in `~/.claude/settings.json` by the installer. Nothing
  is copied to `~/.delegation_core/hooks/` any more.
- **Local install on this machine:** editable install in `~/.delegation_core/venv`,
  daemon as the `delegation-core.service` systemd user unit, dashboard installed as an
  application (`~/.local/share/applications/delegation-core-dashboard.desktop`).

## Architecture notes that matter

- **One HTTP daemon per machine** (`delegation-core run`, since v0.11) serves every
  client: Claude Code, Codex and Antigravity over HTTP with a mandatory bearer token,
  Claude Desktop through `delegation-core mcp-stdio` (`stdio_bridge.py`). One BGE copy
  in memory instead of one per client.
- **The dashboard API lives inside the daemon** (`dashboard_api.serve_in_process`, on
  `dashboard_port`). The Tauri app talks to it, and only spawns its own
  `dashboard_api` as a fallback when the daemon does not answer.
- **CLI commands that write the index hand the work to the daemon** (`daemon.py`)
  instead of opening a second writer. `doctor` checks this (`index_writers`).
- **Generation goes through `engine.py` only** (`tests/test_motor_unico.py`). Work for
  the local model can be queued by any connected agent (`localqueue.py`), drained one
  task at a time by `localworker.py`; `gpu.py` keeps BGE and the local model from
  fighting over one GPU.
- **The index heals itself.** An index that kills the process opening it goes to
  quarantine and is rebuilt from the vault and the ingest sources (`recuperacao.py`),
  never restored from a backup. An event-loop watchdog restarts a wedged daemon.
- **No import cycles in the core** (`tests/test_sem_ciclos_de_import.py`); `notes.py`
  is the bottom layer.
- Frontend is vanilla JS under a strict CSP (`default-src 'self'`); untrusted strings
  go through `escapeHtml()`.

## Recent history

`CHANGELOG.md` is the record. In short, since v0.13.0 (2026-09-04): the macOS
deadlock and self-healing index (PRs 7 to 11, 2026-09-29), the default port 8797
(PR 12), local `compress` fixed (PR 13), Windows blocked-DLL diagnostics and a
`degraded` heartbeat when search is down (PR 14), images as searchable notes by EXIF
and OCR (PR 15), and the v0.14.0 maintenance pass (no import cycles, static analysis
in the suite, internal function no longer exposed as a tool, transcript titles).

## Hard rules / invariants

- **Never touch the user's vault** (path in `~/.delegation_core/config.json` →
  `/home/joey/Documents/Projects_Archive/Claude Vault`) or `~/.delegation_core/models/`
  from installers/uninstallers/tests. The uninstallers hard-abort if the vault resolves
  under `~/.delegation_core`.
- Tests must stay offline/fast; no test may touch the real process table for termination,
  the real vault, or real model loading.
- Dashboard identifier `com.delegationcore.dashboard` must not change (orphans installs).
- When testing the app on this machine, close the window cleanly or SIGKILL is fine now
  (watchdog reaps the sidecar), but always verify no `dashboard_api` processes linger.

## Known limitations / open items

- **Draft release unpublished**: awaiting review/publish decision.
- **macOS build is aarch64-only** (`macos-latest` = ARM runners). Add `macos-13` or an
  `x86_64-apple-darwin` target for Intel Macs if wanted.
- **Unsigned builds**: SmartScreen/Gatekeeper warn on first launch. Signing needs paid certs.
- **AppImage cannot build on this dev machine** (sandboxed linuxdeploy/FUSE); builds fine
  in CI. Not a code bug.
- **No real dpkg/rpm install test yet**: needs an actual Debian/Fedora box or VM.
- **No auto-update** (Tauri updater plugin), a deliberate deferral.

### Open items from the 2026-08-03 graph/vault fixes

Found while ingesting a 7.7k-file repository (115.756 graph nodes). Diagnosed and
recorded, deliberately **not** fixed in that change (see the CHANGELOG).

- **`remap_communities_to_previous()` has no caller.** Same class as the bug fixed
  in that change (`label_communities_by_hub` was dead code for months). It exists to
  keep community IDs stable across rebuilds; instead `graphbridge` works around the
  instability by deleting and re-filing every vault article on every rebuild; the
  code comments say so explicitly. Wiring it changes rebuild behaviour, so it wants
  its own change and its own test.
- **Five more "fabricating" fallbacks unaudited.** `x = x or <default>` appears 14
  times in `src/`; most degrade to empty and are harmless, but seven invent a
  plausible value the consumer cannot distinguish from a real one (the two fixed were
  `graph/wiki.py:269` and `config.py:160`). The rest are unreviewed.
- **`Config.load()` still degrades silently.** It falls back to `cls()` on any read
  error, so a corrupt `config.json` yields empty `vault_path`/`llama_binary`/
  `llama_model`. `VaultManager._init()` now refuses the empty vault case; the llama
  fields have no equivalent guard.
- **`community_labels` is still an optional parameter** on the four graph exporters,
  which is what let the caller omit it unnoticed. Left optional on purpose: `graph/`
  is a vendored copy of Graphify and changing upstream signatures makes re-vendoring
  harder. The seam test (`test_build_graph_labels_communities_by_hub_in_every_artifact`)
  is what prevents recurrence instead.
- **Vault index vs note count (mostly explained, 2026-10-03).** `heartbeat()` now
  reports `indexed_notes` and `indexed_rows` separately. Rows are chunks (notes are
  split since v0.12). `indexed_notes` is vault notes plus ingested external files:
  measured 10.533 + 5.939 = 16.472 against 16.481 reported. The remaining handful
  was not investigated.

## How to do things

```bash
# Tests
~/.delegation_core/venv/bin/python3 -m pytest tests/ -q

# Dashboard dev / build
cd dashboard && npm run tauri dev      # dev (WEBKIT_DISABLE_DMABUF_RENDERER=1 is set in lib.rs, needed on NVIDIA/Wayland)
cd dashboard && npm run tauri build    # .deb/.rpm locally; AppImage only in CI

# Release pipeline
git tag dashboard-vX.Y.Z && git push origin dashboard-vX.Y.Z   # builds 3 platforms, attaches to a draft release

# Sanity check the machine
nvidia-smi                             # llama.cpp eats ~11.3GB when on; BGE falls back to CPU if full
ps aux | grep dashboard_api            # should be empty when no dashboard window is open
```
