"""
graph_hook_rebuild.py — entry point invoked by the git post-commit hook
installed by graph_hook.py: `python -m delegation_core.graph_hook_rebuild <repo_root> [name]`.

Runs detached in the background (see graph_hook.py's hook script). Rebuilds the
on-disk graph artifacts only (file_to_vault=False) — no LLM, no vault writes,
no maintenance pass. A simple non-blocking lock file skips a rebuild if one is
already running for this graph rather than piling up (Graphify's watch.py has
a much more elaborate queue/drain mechanism for the same problem; skipping is
an acceptable simplification here since the *next* commit's rebuild covers
everything anyway).
"""

from __future__ import annotations

import asyncio
import os
import platform
import sys
import time
from pathlib import Path

# A hard-killed rebuild (OOM-killer — this process explicitly nices/caps itself
# to be a preferred kill target — SIGKILL, or a host crash) never reaches the
# `finally: os.unlink(lock_path)` cleanup, so the lock file is left behind
# forever. Without a staleness check, every subsequent post-commit hook would
# see FileExistsError and silently skip the rebuild indefinitely, with no
# surfaced error beyond a line in graph_rebuild.log nobody is watching.
_LOCK_MAX_AGE_SECONDS = 3600


def _lock_is_stale(lock_path: Path) -> bool:
    """A lock is stale if it has aged past the cutoff, or its owning PID is
    provably dead. Liveness comes from psutil (a declared dependency), which
    answers the same question on every platform. The signal-0 kill is only a
    POSIX fallback: on Windows os.kill(pid, 0) is not a probe, it terminates
    the process."""
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        return True
    if age > _LOCK_MAX_AGE_SECONDS:
        return True
    try:
        conteudo = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return True

    if not conteudo:
        # Lock recem-criado, ainda sem PID escrito.
        #
        # O lock nasce em duas syscalls: `os.open(O_CREAT|O_EXCL)` e depois
        # `os.write(pid)`. Entre as duas o arquivo existe e esta VAZIO. A versao
        # anterior fazia `int("")`, caia no ValueError e devolvia True: um
        # segundo processo nessa janela declarava o lock obsoleto, apagava, e
        # tomava para si. Os dois rebuilds passavam a escrever no mesmo
        # `graphs_dir/<nome>/` ao mesmo tempo, que e exatamente a colisao que o
        # lock existe para impedir.
        #
        # A janela e de microssegundos e o teste de idade acima nao a fecha,
        # porque um lock recem-criado tem idade ~0 e nao passa do corte. Um
        # arquivo vazio e novo significa "alguem acabou de pegar", nao
        # "abandonado": so vira obsoleto pelo corte de idade.
        return False

    try:
        pid = int(conteudo)
    except ValueError:
        return True
    try:
        import psutil
        return not psutil.pid_exists(pid)
    except ImportError:  # pragma: no cover - psutil e dependencia declarada
        if platform.system() == "Windows":
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _apply_resource_limits() -> None:
    """Best-effort nice + memory cap so a background rebuild doesn't starve
    whatever else is running (e.g. an editor, the MCP server itself).
    Silently skips whatever the platform doesn't support.
    """
    try:
        os.nice(10)
    except (OSError, AttributeError):
        pass
    mb = os.environ.get("DELEGATION_CORE_GRAPH_REBUILD_MEMORY_LIMIT_MB", "").strip()
    if not mb:
        return
    try:
        limit = int(mb) * 1024 * 1024
    except ValueError:
        return
    try:
        import resource
        which = resource.RLIMIT_DATA if sys.platform == "darwin" else resource.RLIMIT_AS
        soft, hard = resource.getrlimit(which)
        new_hard = hard if hard != resource.RLIM_INFINITY and hard < limit else limit
        resource.setrlimit(which, (limit, new_hard))
    except (ImportError, ValueError, OSError):
        pass


def main() -> int:
    if len(sys.argv) < 2:
        sys.stderr.write("usage: python -m delegation_core.graph_hook_rebuild <repo_root> [name]\n")
        return 2

    repo_root = sys.argv[1]
    name = sys.argv[2] if len(sys.argv) > 2 else None

    _apply_resource_limits()

    from .config import Config
    from . import graphbridge

    cfg = Config.load()
    graph_name = graphbridge._slugify(name or __import__("pathlib").Path(repo_root).resolve().name)
    lock_path = cfg.graphs_dir / graph_name / ".rebuild.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if not _lock_is_stale(lock_path):
            sys.stderr.write(f"graph_hook_rebuild: rebuild already in progress for '{graph_name}' — skipping\n")
            return 0
        sys.stderr.write(f"graph_hook_rebuild: clearing stale lock for '{graph_name}'\n")
        try:
            lock_path.unlink()
        except OSError:
            pass
        try:
            lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            sys.stderr.write(f"graph_hook_rebuild: rebuild already in progress for '{graph_name}' — skipping\n")
            return 0

    try:
        os.write(lock_fd, str(os.getpid()).encode())
        os.close(lock_fd)
        result = asyncio.run(
            graphbridge.build_graph(cfg, None, repo_root, name=name, force=True, file_to_vault=False)
        )
        if "error" in result:
            sys.stderr.write(f"graph_hook_rebuild: {result['error']}\n")
            return 1
        sys.stderr.write(
            f"graph_hook_rebuild: rebuilt '{result['name']}' — "
            f"{result['node_count']} nodes, {result['edge_count']} edges\n"
        )
        return 0
    finally:
        try:
            os.unlink(lock_path)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
