"""service.py + clients.py: daemon lifecycle and client migration.

Both modules exist because v0.11 broke two assumptions that stdio made for free:
something has to *start* a daemon before the first client connects, and every
existing client config points at the wrong thing until it is rewritten.

The generated service definitions are checked structurally rather than by
installing them — the systemd unit and the launchd plist are validated for real
in the repo by systemd-analyze/plistlib, but a test suite must not enable a
background service on the machine running it.
"""

import json
import plistlib

import pytest

from delegation_core import clients, service
from delegation_core.config import Config
from delegation_core.windows import SELF


def _cfg():
    return Config(vault_path="/tmp", server_host="127.0.0.1",
                  server_path="/mcp", server_token="tok-abc")


# ── service definitions ──────────────────────────────────────────────────────

def test_systemd_unit_starts_the_daemon_by_absolute_path(monkeypatch):
    """`which delegation-core` finds nothing on a venv install — the console
    script lives in the venv's bin. A service manager's environment is thinner
    than a shell's, so a bare name here is a service that never starts."""
    monkeypatch.setattr(
        service, "_executable", lambda: "/opt/delegation-core/bin/delegation-core"
    )
    unit = service.systemd_unit_text()
    exec_line = next(l for l in unit.splitlines() if l.startswith("ExecStart="))
    path = exec_line.split("=", 1)[1].rsplit(" run", 1)[0]
    assert path.startswith("/")
    assert unit.rstrip().endswith("WantedBy=default.target")


def test_systemd_start_limits_are_unit_keys_not_service_keys():
    """systemd moved StartLimit* to [Unit]; under [Service] it reports
    "Unknown key ... ignoring" and the crash-loop guard silently does nothing."""
    # Split on the section header itself, not on any mention of "[Service]" —
    # the unit's own comment names it, which is enough to fool a naive split.
    unit_section, service_section = service.systemd_unit_text().split("\n[Service]\n", 1)
    directives = [l for l in service_section.splitlines() if not l.startswith("#")]
    assert "StartLimitIntervalSec" in unit_section
    assert "StartLimitBurst" in unit_section
    assert not any("StartLimit" in l for l in directives)


def test_launchd_plist_parses_and_runs_at_load(monkeypatch):
    monkeypatch.setattr(
        service, "_executable", lambda: "/opt/delegation-core/bin/delegation-core"
    )
    parsed = plistlib.loads(service.launchd_plist_text().encode())
    assert parsed["Label"] == service.LAUNCHD_LABEL
    assert parsed["ProgramArguments"][-1] == "run"
    assert parsed["ProgramArguments"][0].startswith("/")
    assert parsed["RunAtLoad"] is True


def test_status_reports_manager_and_reachability_separately(monkeypatch):
    """They answer different questions: an active unit may still be loading BGE,
    and a live port says nothing about surviving a reboot."""
    monkeypatch.setattr(service, "_port_answers", lambda: False)
    monkeypatch.setattr(service, "_run", lambda cmd: (3, "inactive"))
    result = service.status()
    assert result["reachable"] is False
    assert "installed" in result and "manager_state" in result


# ── client migration ─────────────────────────────────────────────────────────

def test_claude_code_entry_is_http_with_bearer_header():
    entry = clients.claude_code_entry(_cfg())
    assert entry["type"] == "http"
    assert entry["url"] == "http://127.0.0.1:8797/mcp"
    assert entry["headers"]["Authorization"] == "Bearer tok-abc"


def test_install_claude_code_replaces_stdio_entry_and_keeps_projects(monkeypatch, tmp_path):
    """The stdio entry must go: left in place it spawns a second daemon that
    fights the real one for the port, the index and the GPU."""
    import delegation_core.windows as windows_mod
    client_config = tmp_path / "claude.json"
    client_config.write_text(json.dumps({
        "mcpServers": {
            SELF: {"command": "/old/path/delegation-core", "args": ["run"]},
            "clickup": {"command": "npx", "args": ["clickup-mcp"]},
        },
        "projects": {"/some/repo": {"mcpServers": {"scoped": {"command": "s"}}}},
    }), encoding="utf-8")
    monkeypatch.setattr(windows_mod, "CLIENT_CONFIG", client_config)

    result = clients.install_claude_code(_cfg())
    after = json.loads(client_config.read_text())

    assert result["replaced"]["command"] == "/old/path/delegation-core"
    assert after["mcpServers"][SELF]["url"] == "http://127.0.0.1:8797/mcp"
    assert "command" not in after["mcpServers"][SELF]
    # Untouched neighbours — the invariants windows.py already guarantees.
    assert after["mcpServers"]["clickup"] == {"command": "npx", "args": ["clickup-mcp"]}
    assert after["projects"]["/some/repo"]["mcpServers"]["scoped"] == {"command": "s"}


def test_install_codex_creates_config_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(clients, "CODEX_CONFIG", tmp_path / "config.toml")
    result = clients.install_codex(_cfg())

    assert result["status"] == "created"
    written = (tmp_path / "config.toml").read_text()
    assert f"[mcp_servers.{SELF}]" in written
    assert 'url = "http://127.0.0.1:8797/mcp"' in written
    # Codex reads the secret from the environment, not from its config file.
    assert clients.CODEX_TOKEN_ENV_VAR in written
    assert "tok-abc" not in written


def test_install_codex_appends_without_disturbing_existing_tables(monkeypatch, tmp_path):
    codex_config = tmp_path / "config.toml"
    codex_config.write_text(
        '[mcp_servers.context7]\ncommand = "npx"\n', encoding="utf-8")
    monkeypatch.setattr(clients, "CODEX_CONFIG", codex_config)

    result = clients.install_codex(_cfg())
    written = codex_config.read_text()

    assert result["status"] == "appended"
    assert "[mcp_servers.context7]" in written
    assert f"[mcp_servers.{SELF}]" in written
    assert (tmp_path / "config.toml.dc-backup").exists()


def test_install_codex_refuses_to_rewrite_an_existing_block(monkeypatch, tmp_path):
    """No TOML writer in the stdlib, so editing a table in place means
    hand-splicing text around a read-only parser. Report instead of corrupt."""
    codex_config = tmp_path / "config.toml"
    original = f'[mcp_servers.{SELF}]\nurl = "http://127.0.0.1:9999/mcp"\n'
    codex_config.write_text(original, encoding="utf-8")
    monkeypatch.setattr(clients, "CODEX_CONFIG", codex_config)

    result = clients.install_codex(_cfg())

    assert result["status"] == "already_present"
    assert codex_config.read_text() == original      # untouched
    assert "8797" in result["block"]                 # the replacement is offered


def test_windows_registry_round_trips_an_http_spec(monkeypatch, tmp_path):
    """windows.py stores specs verbatim, so HTTP entries survive close/open just
    like stdio ones did. delegation-core itself is now HTTP-shaped."""
    import delegation_core.windows as windows_mod
    client_config = tmp_path / "claude.json"
    registry = tmp_path / "registry.json"
    http_spec = {"type": "http", "url": "https://mcp.example.com/mcp"}
    client_config.write_text(json.dumps({"mcpServers": {"remote": http_spec}}),
                             encoding="utf-8")
    monkeypatch.setattr(windows_mod, "CLIENT_CONFIG", client_config)
    monkeypatch.setattr(windows_mod, "REGISTRY", registry)

    windows_mod.close_window("remote")
    assert "remote" not in json.loads(client_config.read_text())["mcpServers"]

    windows_mod.open_window("remote")
    assert json.loads(client_config.read_text())["mcpServers"]["remote"] == http_spec


# ── Antigravity / Gemini CLI ──────────────────────────────────────────────────
# Its config ships empty (0 bytes, untouched since creation on this machine),
# which is the case json.load raises on — so "empty" has to mean "no servers
# yet", not "corrupt".

def _agy_cfg(tmp_path, monkeypatch):
    from delegation_core import clients
    path = tmp_path / "gemini" / "config" / "mcp_config.json"
    monkeypatch.setattr(clients, "ANTIGRAVITY_CONFIG", path)
    return path


def test_antigravity_entry_uses_serverurl_not_url():
    """Antigravity's schema names the remote field serverUrl; an `url` key is
    silently not a server."""
    from delegation_core import clients
    from delegation_core.config import Config

    cfg = Config(server_token="tok")
    entry = clients.antigravity_entry(cfg)
    assert entry["serverUrl"] == cfg.server_url
    assert "url" not in entry
    assert entry["headers"]["Authorization"] == "Bearer tok"


def test_installs_into_a_missing_file(tmp_path, monkeypatch):
    import json
    from delegation_core import clients
    from delegation_core.config import Config

    path = _agy_cfg(tmp_path, monkeypatch)
    result = clients.install_antigravity(Config(server_token="tok"))
    assert result["status"] == "installed"
    assert json.loads(path.read_text())["mcpServers"]["delegation-core"]["serverUrl"]


def test_installs_into_the_empty_file_it_actually_ships_with(tmp_path, monkeypatch):
    import json
    from delegation_core import clients
    from delegation_core.config import Config

    path = _agy_cfg(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text("")            # 0 bytes, exactly as found on disk
    assert clients.install_antigravity(Config(server_token="tok"))["status"] == "installed"
    assert "delegation-core" in json.loads(path.read_text())["mcpServers"]


def test_other_servers_are_preserved(tmp_path, monkeypatch):
    import json
    from delegation_core import clients
    from delegation_core.config import Config

    path = _agy_cfg(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"mcpServers": {"sqlite-helper": {"command": "x"}}}))
    clients.install_antigravity(Config(server_token="tok"))
    servers = json.loads(path.read_text())["mcpServers"]
    assert servers["sqlite-helper"] == {"command": "x"}
    assert "delegation-core" in servers


def test_reinstall_is_idempotent(tmp_path, monkeypatch):
    from delegation_core import clients
    from delegation_core.config import Config

    _agy_cfg(tmp_path, monkeypatch)
    cfg = Config(server_token="tok")
    clients.install_antigravity(cfg)
    assert clients.install_antigravity(cfg)["status"] == "already-configured"


def test_a_rotated_token_is_written_through(tmp_path, monkeypatch):
    import json
    from delegation_core import clients
    from delegation_core.config import Config

    path = _agy_cfg(tmp_path, monkeypatch)
    clients.install_antigravity(Config(server_token="old"))
    assert clients.install_antigravity(Config(server_token="new"))["status"] == "updated"
    entry = json.loads(path.read_text())["mcpServers"]["delegation-core"]
    assert entry["headers"]["Authorization"] == "Bearer new"


def test_malformed_config_is_refused_not_overwritten(tmp_path, monkeypatch):
    """The file may hold another client's servers. Clobbering them to add ours
    is worse than reporting it."""
    from delegation_core import clients
    from delegation_core.config import Config

    path = _agy_cfg(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text("{ not json")
    result = clients.install_antigravity(Config(server_token="tok"))
    assert result["status"] == "error"
    assert path.read_text() == "{ not json"


# ── Claude Desktop ────────────────────────────────────────────────────────────
#
# As duas asercoes originais desta secao exigiam `type: "http"` e um header
# `Authorization` dentro de claude_desktop_config.json. Elas descreviam o que o
# codigo fazia, nao o que o Claude Desktop aceita, e ficaram verdes enquanto a
# funcionalidade estava quebrada em campo: o Desktop recusa entrada sem
# `command` e, pior, com um campo `url` presente ele reescreve o arquivo na
# inicializacao e descarta a secao mcpServers inteira.
#
# Reescritas para exigir a forma que funciona. Ver tests/test_claude_desktop_entry.py.

def test_claude_desktop_installs_into_missing_file(tmp_path):
    from delegation_core import clients
    from delegation_core.config import Config

    path = tmp_path / "Claude" / "claude_desktop_config.json"
    result = clients.install_claude_desktop(Config(server_token="desktop-tok"), target_path=path)
    assert result["status"] == "installed"
    assert path.exists()
    data = json.loads(path.read_text())
    entrada = data["mcpServers"]["delegation-core"]
    assert entrada["args"] == ["mcp-stdio"], "o Desktop so valida stdio"
    assert "url" not in entrada, "url faz o Desktop apagar mcpServers inteiro"


def test_claude_desktop_preserves_other_servers(tmp_path):
    from delegation_core import clients
    from delegation_core.config import Config

    path = tmp_path / "Claude" / "claude_desktop_config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"mcpServers": {"other-server": {"command": "other"}}}))

    result = clients.install_claude_desktop(Config(server_token="desktop-tok"), target_path=path)
    assert result["status"] == "installed"
    data = json.loads(path.read_text())
    assert data["mcpServers"]["other-server"] == {"command": "other"}
    entrada = data["mcpServers"]["delegation-core"]
    assert entrada["args"] == ["mcp-stdio"]
    assert "desktop-tok" not in json.dumps(entrada), "o token nao vai para este arquivo"


def test_claude_desktop_reinstall_is_idempotent(tmp_path):
    from delegation_core import clients
    from delegation_core.config import Config

    path = tmp_path / "Claude" / "claude_desktop_config.json"
    cfg = Config(server_token="tok")
    clients.install_claude_desktop(cfg, target_path=path)
    assert clients.install_claude_desktop(cfg, target_path=path)["status"] == "already-configured"
