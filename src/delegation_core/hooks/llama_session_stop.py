#!/usr/bin/env python3
"""
llama_session_stop.py — Claude Code SessionEnd hook.

Stops the local llama.cpp server when a Claude Code session closes, so the model
does not sit holding VRAM after the person walks away. On this machine that is
~11 GB of a 16 GB card.

WHY THIS IS A SEPARATE ACTOR FROM gpu.release_llama()

`gpu.release_llama()` deliberately refuses to stop a llama-server it did not
spawn: "the arbiter does not kill what it did not start". That boundary is right
for an in-process arbiter deciding who gets the card mid-run.

This hook is a different actor with a different, explicit contract: at the end of
a session, if nothing else is using the model and it is not mid-generation, stop
it regardless of who started it. It is opt-out, it never kills a process whose
command line does not match the configured binary AND port, and every guard that
fires is written to the log with its reason.

GUARDS, all measured rather than assumed:

1. Sentry file `~/.delegation_core/no_auto_llama_stop` exists  -> skip.
2. Nothing listening on the configured port                    -> nothing to do.
3. `/slots` reports any slot with is_processing true           -> skip, mid-work.
4. Another MCP client session file is fresh                    -> skip.
5. The pid's command line does not match binary + port         -> skip, not ours.

KNOWN LIMIT OF GUARD 4

Session heartbeat files go stale after 120 s of no MCP traffic, and a session can
be alive and idle. Measured on 2026-09-09: five session files, all stale, while
one of those sessions was in fact open and working through shell tools. So guard 4
catches a *busy* neighbour, not an idle one. Guard 3 is the one that protects work
in flight. If you run two agents side by side and want the model to survive the
first one closing, use the sentry file.

Only stdlib. Runs as the second step of `delegation-core-hook session-end`
(see entrada.py), after the transcript export, in the same process.
"""

import json
import os
import signal
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CONFIG_PATH = Path.home() / ".delegation_core" / "config.json"
SESSIONS_DIR = Path.home() / ".delegation_core" / "sessions"
SENTRY = Path.home() / ".delegation_core" / "no_auto_llama_stop"
LOG = Path.home() / ".delegation_core" / "llama_stop.log"

SESSION_FRESH_SECONDS = 120      # same threshold client_tracking.py uses
TERM_GRACE_SECONDS = 10.0        # how long to wait for a clean SIGTERM exit


def log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def http_json(url: str, timeout: float = 3.0):
    """GET and parse JSON, or None. Never raises."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def is_busy(port: int) -> bool | None:
    """True/False if /slots answered, None when the endpoint is unavailable.

    None is not the same as False: an unreachable /slots means we could not
    check, and the caller treats that as "do not stop".
    """
    slots = http_json(f"http://127.0.0.1:{port}/slots")
    if not isinstance(slots, list):
        return None
    return any(bool(s.get("is_processing")) for s in slots if isinstance(s, dict))


def other_session_fresh(my_session_id: str) -> bool:
    """Whether any MCP client session other than this one is still fresh."""
    if not SESSIONS_DIR.exists():
        return False
    now = datetime.now(timezone.utc)
    mine = (my_session_id or "").strip()
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sid = str(d.get("session_id") or f.stem)
        if mine and (mine.startswith(sid) or sid.startswith(mine)):
            continue
        try:
            age = (now - datetime.fromisoformat(d["last_seen"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= age < SESSION_FRESH_SECONDS:
            log(f"guarda 4: sessao {d.get('client_name','?')} viva ha {age:.0f}s")
            return True
    return False


def find_pid(binary: str, port: int) -> int | None:
    """The pid whose cmdline holds both the configured binary and port.

    Reads /proc directly so the hook stays stdlib-only. Returns None on any
    platform without /proc, which is the safe answer: we do not stop what we
    cannot positively identify.
    """
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    name = Path(binary).name
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        parts = [p for p in cmdline.split("\0") if p]
        if not parts:
            continue
        if name in Path(parts[0]).name and str(port) in parts:
            return int(entry.name)
    return None


def stop(pid: int) -> str:
    """SIGTERM, wait, then SIGKILL. Returns what actually happened."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "ja tinha saido"
    except PermissionError:
        return "sem permissao para sinalizar"

    deadline = time.monotonic() + TERM_GRACE_SECONDS
    while time.monotonic() < deadline:
        time.sleep(0.25)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "parado com SIGTERM"
        except PermissionError:
            return "sem permissao para conferir"
    try:
        os.kill(pid, signal.SIGKILL)
        return f"nao saiu em {TERM_GRACE_SECONDS:.0f}s, morto com SIGKILL"
    except ProcessLookupError:
        return "parado com SIGTERM no limite"
    except PermissionError:
        return "sem permissao para SIGKILL"


def main(raw: str | None = None) -> int:
    session_id = ""
    try:
        if raw is None:
            raw = sys.stdin.read()
        raw = raw.strip()
        if raw:
            session_id = str(json.loads(raw).get("session_id", ""))
    except Exception:
        pass

    if SENTRY.exists():
        log(f"guarda 1: {SENTRY.name} presente, nao mexo no llama-server")
        return 0

    cfg = load_config()
    port = int(cfg.get("llama_port", 8181))
    binary = cfg.get("llama_binary", "llama-server")

    if http_json(f"http://127.0.0.1:{port}/health") is None:
        log(f"guarda 2: nada respondendo em {port}, nada a fazer")
        return 0

    busy = is_busy(port)
    if busy is None:
        log("guarda 3: /slots nao respondeu, nao da para saber se esta ocupado, nao paro")
        return 0
    if busy:
        log("guarda 3: slot processando, deixo rodando")
        return 0

    if other_session_fresh(session_id):
        return 0

    pid = find_pid(binary, port)
    if pid is None:
        log(f"guarda 5: nenhum processo com {Path(binary).name} e porta {port}, nao paro nada")
        return 0

    log(f"parando llama-server pid={pid} porta={port} sessao={session_id[:8] or '?'}: {stop(pid)}")
    return 0
