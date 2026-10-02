"""
clients.py: point MCP clients at the HTTP daemon.

v0.11 replaced stdio with a single HTTP daemon, and that is a breaking change for
every client already configured: a `{"command": ..., "args": ["run"]}` entry now
spawns a *second* server that will fight the daemon for the port, the ChromaDB
index, and the GPU. There is no version of "it keeps working by accident", so the
migration is explicit and this module is what performs it.

Three clients are handled directly because they are the ones on this machine.
Everything else is covered by `entry_summary()`, which just describes the URL and
token so a user can paste them wherever their client wants them.

Claude Code (~/.claude.json, JSON):

    "delegation-core": {
      "type": "http",
      "url": "http://127.0.0.1:8797/mcp",
      "headers": {"Authorization": "Bearer <token>"}
    }

Codex (~/.codex/config.toml, TOML):

    [mcp_servers.delegation-core]
    url = "http://127.0.0.1:8797/mcp"
    bearer_token_env_var = "DELEGATION_CORE_TOKEN"

Codex reads the secret from an environment variable rather than the config file,
so migrating it also means telling the user to export DELEGATION_CORE_TOKEN. That
is Codex's design, not a choice made here.

Antigravity / Gemini CLI (~/.gemini/config/mcp_config.json, JSON):

    "delegation-core": {
      "serverUrl": "http://127.0.0.1:8797/mcp",
      "headers": {"Authorization": "Bearer <token>"}
    }

The key is `serverUrl`, not `url`: Antigravity's own embedded documentation
describes exactly two transports, stdio (`command`/`args`/`env`) and remote
(`serverUrl`), and calls the remote one SSE. This daemon serves streamable HTTP
at the same path, which most current clients accept under that field; whether
this one does is a question for a live connection, not for this docstring.
"""

from __future__ import annotations

import json
import logging
import platform
import shutil
from pathlib import Path

from .config import Config
from .windows import SELF, read_client_config, write_client_config

logger = logging.getLogger("clients")

CODEX_CONFIG = Path.home() / ".codex" / "config.toml"

#: Antigravity (the `agy` CLI) and the Gemini CLI share this file. Its own docs
#: call it the "Global Configuration", applying to all sessions.
ANTIGRAVITY_CONFIG = Path.home() / ".gemini" / "config" / "mcp_config.json"


def claude_desktop_config_path() -> Path:
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    elif system == "Windows":
        return Path.home() / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
    else:
        return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"

#: Codex looks the bearer token up in the environment under this name.
CODEX_TOKEN_ENV_VAR = "DELEGATION_CORE_TOKEN"


def claude_code_entry(cfg: Config) -> dict:
    """The ~/.claude.json mcpServers value pointing at the daemon."""
    return {
        "type": "http",
        "url": cfg.server_url,
        "headers": {"Authorization": f"Bearer {cfg.server_token}"},
    }


def claude_desktop_entry(cfg: Config) -> dict:
    """The claude_desktop_config.json value. stdio, and never a URL.

    Its own function instead of reusing `claude_code_entry`, which is the
    mistake this replaces: the two files look alike and validate differently.
    `~/.claude.json` accepts `type: http`; `claude_desktop_config.json` does
    not, and an entry carrying `url` there makes Desktop rewrite the file on
    startup and drop the whole `mcpServers` section plus some `preferences`
    keys, with no error. So the wrong shape here does not just fail to connect,
    it can delete every other MCP server the user had configured.

    stdio, but NOT `{"command": ..., "args": ["run"]}`: that starts a second
    daemon fighting the first for the port, the index and the GPU. `mcp-stdio`
    is the bridge in stdio_bridge.py, which opens nothing and forwards every
    call to the one daemon.
    """
    from . import service
    return {"command": service._executable(), "args": ["mcp-stdio"]}


def codex_block(cfg: Config) -> str:
    """The ~/.codex/config.toml table pointing at the daemon."""
    return (
        f"\n[mcp_servers.{SELF}]\n"
        f'url = "{cfg.server_url}"\n'
        f'bearer_token_env_var = "{CODEX_TOKEN_ENV_VAR}"\n'
        f"startup_timeout_sec = 30\n"
        f"tool_timeout_sec = 120\n"
    )


def antigravity_entry(cfg: Config) -> dict:
    """The ~/.gemini/config/mcp_config.json mcpServers value for the daemon."""
    return {
        "serverUrl": cfg.server_url,
        "headers": {"Authorization": f"Bearer {cfg.server_token}"},
    }


def install_antigravity(cfg: Config) -> dict:
    """Point Antigravity / the Gemini CLI at the daemon.

    The file ships empty (0 bytes on this machine, untouched since it was
    created), and json.load on an empty file raises rather than returning {}:
    so emptiness is treated as "no servers yet" instead of as corruption.

    Other servers in the file are preserved; only this one entry is rewritten.
    """
    ANTIGRAVITY_CONFIG.parent.mkdir(parents=True, exist_ok=True)

    data: dict = {}
    if ANTIGRAVITY_CONFIG.exists():
        raw = ANTIGRAVITY_CONFIG.read_text(encoding="utf-8").strip()
        if raw:
            try:
                loaded = json.loads(raw)
                data = loaded if isinstance(loaded, dict) else {}
            except json.JSONDecodeError:
                # Refuse rather than overwrite: this file may hold another
                # client's servers, and clobbering them to add ours is a worse
                # outcome than telling the user to look at it.
                return {
                    "client": "antigravity",
                    "path": str(ANTIGRAVITY_CONFIG),
                    "status": "error",
                    "detail": "mcp_config.json is not valid JSON: not touching it",
                }

    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        return {
            "client": "antigravity",
            "path": str(ANTIGRAVITY_CONFIG),
            "status": "error",
            "detail": "mcpServers is not an object: not touching it",
        }

    before = servers.get(SELF)
    servers[SELF] = antigravity_entry(cfg)
    if before == servers[SELF]:
        return {"client": "antigravity", "path": str(ANTIGRAVITY_CONFIG),
                "status": "already-configured"}

    if ANTIGRAVITY_CONFIG.exists() and ANTIGRAVITY_CONFIG.stat().st_size:
        backup = ANTIGRAVITY_CONFIG.with_suffix(".json.dc-backup")
        if not backup.exists():
            shutil.copy2(ANTIGRAVITY_CONFIG, backup)

    # Atomic: a half-written config leaves the client unable to start, and this
    # one is read by every agy session.
    tmp = ANTIGRAVITY_CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(ANTIGRAVITY_CONFIG)
    return {"client": "antigravity", "path": str(ANTIGRAVITY_CONFIG),
            "status": "updated" if before else "installed"}


def install_claude_desktop(cfg: Config, target_path: Path | None = None) -> dict:
    """Point Claude Desktop at the HTTP daemon in claude_desktop_config.json."""
    config_file = target_path or claude_desktop_config_path()
    config_file.parent.mkdir(parents=True, exist_ok=True)

    data: dict = {}
    if config_file.exists():
        raw = config_file.read_text(encoding="utf-8").strip()
        if raw:
            try:
                loaded = json.loads(raw)
                data = loaded if isinstance(loaded, dict) else {}
            except json.JSONDecodeError:
                return {
                    "client": "claude-desktop",
                    "path": str(config_file),
                    "status": "error",
                    "detail": "claude_desktop_config.json is not valid JSON, not touching it",
                }

    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        return {
            "client": "claude-desktop",
            "path": str(config_file),
            "status": "error",
            "detail": "mcpServers is not an object, not touching it",
        }

    before = servers.get(SELF)
    servers[SELF] = claude_desktop_entry(cfg)
    if before == servers[SELF]:
        return {"client": "claude-desktop", "path": str(config_file),
                "status": "already-configured"}

    # Writing the right entry IS the repair: the dangerous one is the value
    # being replaced. Reported so the user learns their file was at risk, and
    # so a support conversation has something concrete to point at.
    reparado = isinstance(before, dict) and "url" in before

    if config_file.exists() and config_file.stat().st_size:
        backup = config_file.with_suffix(".json.dc-backup")
        if not backup.exists():
            shutil.copy2(config_file, backup)

    tmp = config_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(config_file)
    return {"client": "claude-desktop", "path": str(config_file),
            "status": "updated" if before else "installed",
            "repaired_unsafe_url_entry": reparado}


def install_claude_code(cfg: Config) -> dict:
    """Rewrite delegation-core's entry in ~/.claude.json to the HTTP form.

    Goes through windows.write_client_config so the invariants that module
    already guarantees still hold: one-shot backup before the first edit, atomic
    replace (a half-written ~/.claude.json leaves the client unable to start),
    and the `projects` key untouched.
    """
    client_cfg = read_client_config()
    servers = dict(client_cfg.get("mcpServers") or {})
    previous = servers.get(SELF)
    servers[SELF] = claude_code_entry(cfg)
    client_cfg["mcpServers"] = servers
    write_client_config(client_cfg)
    return {
        "client": "claude-code",
        "path": str(Path.home() / ".claude.json"),
        "replaced": previous,
        "status": "updated",
        "reconnect_required": True,
    }


def install_codex(cfg: Config) -> dict:
    """Append (or report) delegation-core's table in ~/.codex/config.toml.

    Deliberately append-only and refuses to edit an existing table. There is no
    TOML writer in the stdlib, so rewriting a table in place would mean
    hand-editing text around a parser that only reads: the failure mode is a
    corrupted config for a tool the user relies on. Reporting the block and
    letting them replace it is the honest option.
    """
    block = codex_block(cfg)
    if not CODEX_CONFIG.exists():
        CODEX_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CODEX_CONFIG.write_text(block.lstrip("\n"), encoding="utf-8")
        return {"client": "codex", "path": str(CODEX_CONFIG),
                "status": "created", "block": block,
                "env_var": CODEX_TOKEN_ENV_VAR}

    existing = CODEX_CONFIG.read_text(encoding="utf-8")
    if f"[mcp_servers.{SELF}]" in existing:
        return {"client": "codex", "path": str(CODEX_CONFIG),
                "status": "already_present",
                "block": block, "env_var": CODEX_TOKEN_ENV_VAR,
                "note": ("A [mcp_servers.delegation-core] table already exists. "
                         "Replace it by hand with the block above (this command "
                         "will not rewrite TOML it did not write).")}

    backup = CODEX_CONFIG.with_suffix(".toml.dc-backup")
    if not backup.exists():
        shutil.copy2(CODEX_CONFIG, backup)
    CODEX_CONFIG.write_text(existing.rstrip("\n") + "\n" + block, encoding="utf-8")
    return {"client": "codex", "path": str(CODEX_CONFIG), "status": "appended",
            "block": block, "env_var": CODEX_TOKEN_ENV_VAR}


def entry_summary(cfg: Config) -> dict:
    """Everything a client needs, for surfaces this module does not write."""
    return {
        "url": cfg.server_url,
        "authorization_header": f"Bearer {cfg.server_token}",
        "token_env_var": CODEX_TOKEN_ENV_VAR,
    }
