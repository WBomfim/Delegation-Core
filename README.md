# Delegation-Core Office

A local MCP delegation server: a markdown vault (semantic search via BGE + ChromaDB), an
optional local LLM (llama.cpp) for summarization/synthesis, and a vendored code-graph
pipeline - all usable either as an MCP server (Claude Desktop/Code) or directly from a
terminal via the `delegation-core` CLI. It runs as one HTTP daemon that every client shares,
so the model and the index are loaded once per machine rather than once per client.

The idea: push retrieval, classification, and compression work onto a local, zero-marginal-cost
model instead of spending an LLM agent's tokens on it. See `AGENT_GUIDE.md` for the full
protocol an AI agent should follow when this MCP server is connected.

## What it does

- **Vault**: an Obsidian-compatible markdown vault, semantically searchable via BGE
  embeddings + ChromaDB. Drop files into `_inbox/` and they get classified, synthesized into
  clean notes, wikilinked, and filed - or write/read/search notes directly.
- **Code graph** (opt-in, `[graph]` extra): build a knowledge graph of a codebase - AST
  extraction across ~30 languages via tree-sitter, community detection, god-node/blast-radius
  analysis. Produces `graph.json`, an interactive `graph.html`, Mermaid architecture diagrams
  (`callflow.html`), a human-readable `GRAPH_REPORT.md`, and per-community wiki articles filed
  into the vault. Vendored and adapted from
  [Graphify](https://github.com/Graphify-Labs/graphify) - see `THIRD_PARTY_LICENSES/`.
- **Process tracking**: lightweight cross-session task tracking that survives restarts.
- **Dashboard** (`dashboard/`): a native Tauri desktop app - a top status/clients bar, a
  persistent vault-graph pane, a notes browser, and a cross-session task tracker. See
  `dashboard/README.md`.

## Install

The platform installer is the easiest path: it detects Python, creates a venv, installs
delegation-core, installs the native Tauri dashboard app (from a local build if present,
otherwise the latest GitHub release, falling back to manual build instructions if neither
is available), then launches the setup wizard automatically. Re-running it on an existing
install upgrades in place - `config.json` is preserved and the prior install is backed up,
the wizard is not re-run.

```bash
./install.sh              # Linux / macOS
install.bat                # Windows (or double-click)
```

(macOS: `install.command` is a double-clickable Finder shim to `install.sh`.)

To remove everything the installer created - venv, config, logs, hooks - run the matching
uninstaller (`uninstall.sh` / `uninstall.bat` / `uninstall.command`). It never touches your
vault or downloaded model weights.

For a manual/dev install instead (no OS packages, no dashboard app):

```bash
pip install -e .                    # core (vault + MCP server)
pip install -e ".[graph]"           # + code-graph pipeline (~27 tree-sitter language bindings)
pip install -e ".[web]"             # + web search (DuckDuckGo)
pip install -e ".[dev]"             # + pytest, for running tests/
```

Then run the interactive setup wizard once per machine:

```bash
delegation-core setup
```

This finds (or lets you create) an Obsidian vault, downloads/configures a local llama.cpp
model (or lets you skip it - see "engine modes" below), and optionally registers
delegation-core to start automatically (systemd/launchd/Task Scheduler).

### Configure recurring external ingestion

After the first setup, and **before the first ingestion**, configure any recurring,
shared or broad external document source in `~/.delegation_core/config.json`. Do not
pre-create this file before the initial installer/wizard: the wizard owns the initial
machine configuration. The installer preserves it on upgrades.

See [docs/INGEST_CONFIGURATION.md](docs/INGEST_CONFIGURATION.md) for the
`ingest_sources` allow-list, exclusion patterns, safe edits during use, and the
configured ingestion commands.

## Using it as an MCP server

delegation-core runs as a single HTTP daemon on `127.0.0.1:8797`, and every MCP client
connects to that one process. Point your clients at it with:

```bash
delegation-core clients          # writes the http entry + bearer token into known clients
```

The default MCP port is `8797` to avoid a collision with Headroom's commonly used
`8787` proxy port. Existing installations keep the `server_port` saved in
`~/.delegation_core/config.json`. To migrate one, change that setting, restart the
daemon, and refresh the affected clients with `delegation-core clients` (follow
its instructions for an existing Codex entry). The dashboard API port is a
separate setting.

This is a one-time migration for anyone upgrading from v0.10 or earlier, which spoke stdio:
a leftover `{"command": ..., "args": ["run"]}` entry spawns a second server that fights the
daemon for the port, the ChromaDB index, and the GPU. One daemon means one resident copy of
BGE-m3 instead of one per client.

Once connected, ask the running server what it can do - `capabilities()` reports the live
tool list from `mcp.list_tools()` rather than a count written down here that drifts. Full
protocol and tool reference: `AGENT_GUIDE.md`.

## Using it as a CLI

Everything the MCP server exposes is also reachable directly from a terminal:

```bash
delegation-core status                          # vault/model/llama.cpp health
delegation-core search "auth token refresh"      # BGE search, no LLM needed
delegation-core note write Reference "Some note" --file notes.md
delegation-core note read "Some note"
delegation-core graph build ~/code/my-project    # code graph -> vault
delegation-core graph affected my-project auth.py  # blast-radius query
delegation-core graph hook install ~/code/my-project  # auto-rebuild on every commit
delegation-core process create "Migrate auth" --steps "plan,implement,test"
```

Run `delegation-core --help` (and `<command> --help`) for the full tree - `setup`, `run`,
`status`, `reindex`, `maintain`, `ingest`, `relink`, `search`, `compress`, `note`, `graph`,
`process`.

## Engine modes

Set in `config.json` (`engine_mode`), chosen during `setup`:

- **`local`** - summarization/synthesis runs on a local llama.cpp model. Fully offline once
  the model is downloaded.
- **`agent`** - no local model; synthesis/compression is delegated to whichever MCP client
  is calling (e.g. Claude Code). For machines that can't spare the RAM/CPU for a local model.
- **`hybrid`** - light interactive work delegates to the calling agent; heavy/background work
  (maintenance, healing, bulk synthesis) always runs locally.

BGE embeddings + ChromaDB search always run locally in every mode.

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -q
```

The suite is fast and offline, with no ChromaDB/BGE/llama.cpp dependency (the heavier
collaborators are faked), and `tests/conftest.py` keeps a run from touching the real
`~/.delegation_core`. A test total used to be printed here and drifted to roughly half
the real figure; run `pytest tests/ -q` for the number. They cover config, vault helpers and browsing, search scoping,
note rename/delete, the daemon's request routing, the dashboard API's routes and CORS,
client tracking, the graph registry/folder-resolution logic, the git hook installer, and
process tracking. `organizer.py`'s synthesis pipeline is still the notable gap - it needs a
real model to say anything useful.

## More detail

- `AGENT_GUIDE.md` - full MCP tool reference and protocol, written for the AI agent side.
- `CHANGELOG.md` - version history.
- `DEPLOYMENT_LOG.md` - per-deployment upgrade notes (this repo runs on more than one machine).
- `docs/INGEST_CONFIGURATION.md` - declarative ingestion sources, exclusions and safe re-ingestion.
- `THIRD_PARTY_LICENSES/` - attribution for vendored code (Graphify).
