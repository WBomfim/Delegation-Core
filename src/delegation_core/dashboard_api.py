"""
dashboard_api.py — local JSON HTTP API for the Tauri dashboard.

Since v0.11 these routes are served from inside the daemon, on the daemon's own
warm objects — see serve_in_process(). Stdlib http.server only, no new
dependency; it reuses Config/VaultManager/graphbridge/client_tracking/
ProcessTracker directly, and none of this goes through the MCP protocol.

It started as a separate process the Tauri app spawned as a sidecar, which stdio
forced: mcp.run() serves one transport at a time, so the MCP server could not
also serve HTTP for a UI. That separation bought its independence with a second
VaultManager — a second resident BGE-m3 (2314 MiB, measured) plus a second
ChromaDB opener on the index the server already held. The sidecar entry point
below (run()) still works and is still what a Tauri install without the service
falls back to; the daemon is simply the path that does not pay twice.

Bound to 127.0.0.1 only (never a public interface) since it has no auth — it's
meant to be reached exclusively by the local Tauri webview.

Endpoints:
  GET  /api/status                  vault/binary/model/llama.cpp health
  GET  /api/clients                 currently-connected MCP client surfaces
  GET  /api/vault/tree               directory shape (path, depth, note count)
  GET  /api/vault/notes?dir=&offset= notes inside one directory, paginated
  GET  /api/vault/find?q=            literal title/path lookup (no embeddings)
  GET  /api/vault/backlinks?path=    inbound + outbound wikilinks for one note
  GET  /api/vault/note?path=...      one note's raw content
  GET  /api/vault/search?q=...       BGE similarity search
  GET  /api/vault/graph              {nodes, edges} from [[wikilinks]] across the vault
  GET  /api/graphs                   previously built code graphs (graphbridge registry)
  GET  /api/processes?status=&query= tracked processes (ProcessTracker.list_processes)
  GET  /api/processes/get?id=...     full detail of one process
  POST /api/vault/note/create        {folder, title, content} -> new dated note
  POST /api/vault/note/save          {path, content} -> overwrite + reindex
  POST /api/vault/note/rename        {path, new_title} -> rename + repoint links
  POST /api/processes/create         {name, description, steps: [str]}
  POST /api/processes/update         {process_id, note, step_done, status}

Process endpoints read/write the SAME ~/.delegation_core/processes.json the MCP
tools (process_create/list/update/get) and CLI (`delegation-core process ...`)
already use — ProcessTracker's own write-then-rename is what keeps that safe
across processes, same as it always has been; nothing new added here for it.

New in v0.8.0 (GET endpoints only). v0.8.1 added the process write endpoints
(POST /api/processes/create, /update) for the dashboard's Task Tracker panel,
plus two fixes found during that pass's code review rather than from running
anything:
  - CORS was `Access-Control-Allow-Origin: *` unconditionally — this server
    has no auth, so that let ANY website open in the user's regular browser
    read/write vault and process data via a guessed local port, not just the
    Tauri webview. Now allowlists only http(s)://127.0.0.1|localhost:<port>
    and the tauri://localhost / http://tauri.localhost schemes — confirmed by
    directly inspecting the real Origin header the Tauri dev webview sends
    (http://127.0.0.1:1430 on this build) rather than guessing.
  - /api/vault/note's path-containment check was a plain string prefix
    (str(target).startswith(str(vault_root))) — bypassable via a sibling
    directory whose name happens to start with the vault root's own name.
    Fixed with Path.relative_to(); the identical bug existed in server.py's
    relink_folder tool too (fixed there in the same pass).

The sidecar path is also runnable standalone (without Tauri) via:
  delegation-core dashboard-api [--port N]

When spawned by the Tauri app, --parent-pid <tauri pid> is passed and a watchdog
thread exits this process when that PID dies — without it, a hard kill of the
Tauri app (SIGKILL, crash) orphans this sidecar forever, and each orphan holds
the BGE model's ~600MB of GPU memory. No flag (manual/CLI launches) = no watchdog.
"""

from __future__ import annotations

import atexit
import json
import logging
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import notewriter as _notewriter

logger = logging.getLogger("dashboard_api")

#: Valores que a rota de config aceita para `llama_device` (iguais a engine.LLAMA_DEVICES,
#: repetidos aqui para a API nao importar o engine).
DISPOSITIVOS_DO_MODELO = ("auto", "gpu", "cpu")

_cfg = None     # set once in run() or serve_in_process() — Config
_vault = None   # set once in run() — VaultManager, shared across every request.
                # A fresh VaultManager per request (the original version of this
                # file did this) reloads the BGE model and re-opens ChromaDB on
                # every single API call — slow, and observed directly to fail
                # ("Vault not initialized") under repeated init. One shared
                # instance, initialized once at startup, matches server.py's
                # own _vault global pattern.
_tracker = None  # set once in run() — ProcessTracker, same shared-instance reasoning
_engine = None   # lazy DelegationEngine, only for llama.cpp start/stop — see _get_engine()


def _get_engine():
    """Lazily build the DelegationEngine used to start llama.cpp.

    Only used for _start_locked() here (start-if-not-healthy, with the binary/
    model checks and log rotation already implemented there) — stopping goes
    through _find_llama_process() below instead, since DelegationEngine's own
    _shutdown() only kills a process *it* spawned, and llama.cpp on a real
    setup is just as likely to have been started by the MCP server's own
    engine, an autostart service, or by hand (as it was during development of
    this feature) — the dashboard's stop button needs to work regardless.

    DelegationEngine.__init__ registers its own atexit shutdown hook, which
    would kill llama.cpp when *this sidecar process* exits — wrong here, since
    the whole point of a manual start/stop button is that llama.cpp's lifetime
    is under the user's control, not tied to whether the dashboard window
    happens to be open. Unregistered immediately after construction.
    """
    global _engine
    if _engine is None:
        from .engine import DelegationEngine
        _engine = DelegationEngine(_cfg)
        atexit.unregister(_engine._shutdown)
    return _engine


def _find_llama_process():
    """Find the running llama-server process by binary + configured port,
    regardless of what started it. Returns a psutil.Process or None."""
    import psutil

    binary_name = Path(_cfg.llama_binary).name
    port_str = str(_cfg.llama_port)
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmdline = proc.info["cmdline"] or []
            if not cmdline or binary_name not in cmdline[0]:
                continue
            if port_str in cmdline:
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return None


_VAULT_GRAPH_MAX_NODES = 1500


def _build_vault_graph(cfg, include_generated: bool = False,
                       max_nodes: int = _VAULT_GRAPH_MAX_NODES) -> dict:
    """Parse every vault note for [[wikilinks]] and return {nodes, edges}.

    Dependency-free on purpose (no networkx) — the vault graph view shouldn't
    require the [graph] extra, which is for the separate *code* graph pipeline.
    Reuses linker.py's own wikilink regex (existing_targets) rather than
    re-deriving the [[stem|Display]]/[[stem#section]] parsing rules.

    This is the *knowledge* graph. Code graphs are a separate thing with their
    own artifacts (graph.json/graph.html/callflow.html under
    ``~/.delegation_core/graphs/<name>/``), their own API (``/api/graphs``) and
    their own pane behind the dashboard's Vault/Code toggle — so their wiki
    articles are excluded here by default rather than mixed in. They were mixed
    in only because graph_build files them into a vault folder to make them
    searchable: on this vault that meant 3661 generated articles against 216
    hand-written notes, i.e. the "vault" view was 94% one codebase.

    Membership uses ``VaultManager.classify_path`` — the same rule that already
    backs ``search_vault(scope=...)`` — rather than a second, parallel
    definition of what counts as generated.

    ``max_nodes`` additionally caps what reaches the canvas, keeping the newest
    notes; the renderer is force-directed and every node costs per frame.
    ``total_nodes``/``truncated``/``generated_excluded`` are reported so a
    bounded answer is never mistaken for a complete one.
    """
    from .linker import existing_targets
    from .vault import VaultManager, yaml_unquote_scalar

    vault = cfg.vault
    notes: dict[str, dict] = {}   # lowercase stem -> {id, title, folder, path}
    contents: dict[str, str] = {}  # lowercase stem -> raw content (for link extraction)
    generated_skipped = 0

    for folder in cfg.vault_folders:
        folder_path = vault / folder
        if not folder_path.exists():
            continue
        for f in folder_path.rglob("*.md"):
            try:
                content = f.read_text(encoding="utf-8")
            except OSError:
                continue
            title = f.stem
            if content.startswith("---\n"):
                close = content.find("\n---\n", 4)
                if close != -1:
                    for line in content[4:close].splitlines():
                        if line.startswith("title:"):
                            title = yaml_unquote_scalar(line.split(":", 1)[1])
                            break
            rel = f.relative_to(vault).as_posix()
            if not include_generated and VaultManager.classify_path(rel)[0] == "generated":
                generated_skipped += 1
                continue
            key = f.stem.lower()
            notes[key] = {"id": key, "title": title, "folder": folder, "path": rel,
                          "mtime": f.stat().st_mtime}
            contents[key] = content

    total_nodes = len(notes)
    if total_nodes > max_nodes:
        # Keep the newest — an over-cap vault is one where recent work matters
        # more than a note filed months ago. Edges are built after the cut so
        # no edge can point at a node the client never received.
        keep = sorted(notes.items(), key=lambda kv: kv[1]["mtime"], reverse=True)[:max_nodes]
        notes = dict(keep)
        contents = {k: contents[k] for k in notes}
    for node in notes.values():
        node.pop("mtime", None)

    edges = []
    seen_edges = set()
    for key, content in contents.items():
        for target in existing_targets(content):
            target_key = target.strip().lower()
            if target_key == key or target_key not in notes:
                continue  # skip self-links and dangling links (not built this pass)
            edge = (key, target_key)
            if edge in seen_edges:
                continue
            seen_edges.add(edge)
            edges.append({"source": key, "target": target_key})

    return {
        "nodes": list(notes.values()),
        "edges": edges,
        "total_nodes": total_nodes,
        "truncated": total_nodes > len(notes),
        "max_nodes": max_nodes,
        "generated_excluded": generated_skipped,
    }


def _status(cfg, vault) -> dict:
    """Mirrors cli.py's cmd_status checks.

    Reads the note count off the already-open shared `vault` (VaultManager.
    get_stats()) rather than opening a second chromadb.PersistentClient at the
    same path — two clients contending for the same ChromaDB/SQLite path is
    exactly what silently broke search/vault-tree before dashboard_api.py was
    switched to one shared VaultManager instance; opening yet another one here
    just for a count would reintroduce the same class of problem.
    """
    import requests

    vault_ok = Path(cfg.vault_path).exists() if cfg.vault_path else False
    binary_ok = Path(cfg.llama_binary).exists() if cfg.llama_binary else False
    model_ok = Path(cfg.llama_model).exists() if cfg.llama_model else False

    try:
        r = requests.get(f"{cfg.llama_url}/health", timeout=3)
        llama_state = "online" if r.status_code == 200 else "unhealthy"
    except Exception:
        llama_state = "offline"

    chroma_count = vault.get_stats().get("indexed_notes") if vault else None

    # The header used to hardcode this in index.html, where it read v0.9.0
    # against a source tree at 0.10.0 — a copy of the version nothing could keep
    # in sync, and the one users actually look at. Served from the package now.
    from . import __version__

    return {
        "version": __version__,
        "configured": cfg.is_configured(),
        "vault_path": cfg.vault_path,
        "vault_ok": vault_ok,
        "llama_binary": cfg.llama_binary,
        "binary_ok": binary_ok,
        "llama_model": cfg.llama_model,
        "model_ok": model_ok,
        "vault_folders": cfg.vault_folders,
        "llama_url": cfg.llama_url,
        "llama_state": llama_state,
        "chroma_indexed_notes": chroma_count,
        "budget_mode": cfg.budget_mode,
        "engine_mode": cfg.engine_mode,
        "synthesis_enabled": cfg.synthesis_enabled,
    }



#: Nome de campo cujo VALOR nao pode sair por uma rota sem autenticacao.
#: Por forma do nome, e nao por lista fixa: um campo novo chamado
#: `webhook_secret` fica coberto no dia em que for escrito, sem ninguem lembrar.
_PARECE_SEGREDO = ("token", "secret", "password", "api_key", "apikey")

#: As excecoes sao por campo e com motivo, para um campo novo nao entrar de
#: carona: `max_tokens` e um orcamento e `embed_max_seq_length` um limite.
_NAO_E_SEGREDO = {"max_tokens", "embed_max_seq_length"}


def _config_publico(cfg) -> dict:
    """A config como uma rota SEM AUTENTICACAO pode devolve-la.

    `_handle_config_get` fazia `asdict(cfg)` e mandava a dataclass inteira, com
    o `server_token` dentro. Medido nesta maquina em 04/09/2026:

        GET http://127.0.0.1:8788/api/config   -> 200, sem credencial
        ["config"]["server_token"]             -> 43 caracteres, o token inteiro

    E o token que autentica toda chamada ao daemon na 8787: com ele, qualquer um
    chama qualquer ferramenta MCP contra o vault do usuario.

    O CORS ja impedia uma pagina de outro site de LER a resposta -- conferido, e
    `test_dashboard_api_cors.py` fixa isso. Mas o socket esta em 127.0.0.1, que
    QUALQUER usuario local alcanca, enquanto `Config.save()` faz chmod 0600 no
    arquivo com o motivo escrito: "server_token lives in here, so the file is a
    secret now". Um segundo usuario da maquina nao le o arquivo e lia o token
    pelo HTTP: a fronteira que o 0600 desenha nao existia aqui.

    No mesmo par de rotas, o lado que ESCREVE ja tinha lista de permissao campo
    a campo. So o lado que LE despejava tudo.

    Redige em vez de omitir: "nao ha token" e "ha um e nao te mostro" sao
    estados diferentes, e o dashboard precisa distinguir o primeiro.
    """
    from dataclasses import asdict

    saida = asdict(cfg)
    for chave, valor in list(saida.items()):
        if chave in _NAO_E_SEGREDO:
            continue
        if any(p in chave.lower() for p in _PARECE_SEGREDO):
            saida[chave] = "<set>" if valor else ""
    return saida


class _Handler(BaseHTTPRequestHandler):
    # Quiets the default per-request stderr access log; dashboard_api is meant
    # to run silently as a sidecar. Uncomment for debugging.
    def log_message(self, format, *args):
        pass

    def _cors_origin(self) -> str | None:
        """Origin to echo back in Access-Control-Allow-Origin, or None to omit
        the header entirely (browser then blocks the caller from reading the
        response).

        This server has no auth and binds only to 127.0.0.1, but that alone
        doesn't stop a page loaded from a completely different site in the
        user's regular browser from fetch()-ing a guessed local port — CORS is
        what actually stops that page's JS from reading the response (and,
        via the preflight this unlocks, from a state-changing POST executing
        at all). Sending `Access-Control-Allow-Origin: *` — this server's
        original behavior — grants that to literally any website. The only
        legitimate caller is this app's own Tauri webview, whose Origin is
        either an http://127.0.0.1:<port>/http://localhost:<port> dev asset
        server (confirmed directly: `tauri dev` serves the frontend from
        http://127.0.0.1:1430 on this build) or a tauri://localhost /
        http://tauri.localhost production scheme, depending on platform and
        build mode. Allow exactly that shape; nothing else.
        """
        origin = self.headers.get("Origin")
        if not origin:
            return None
        if origin in ("tauri://localhost", "http://tauri.localhost"):
            return origin
        if re.fullmatch(r"http://(127\.0\.0\.1|localhost):\d+", origin):
            return origin
        return None

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        self._serve(self._route_get)

    def do_POST(self):
        self._serve(self._route_post)

    def _serve(self, route) -> None:
        """Run one request's routing under the error handling every verb shares.

        do_GET and do_POST carried byte-identical except: clauses. Both needed
        updating when the disconnect case was split out of the 500 path, and a
        third verb would have had to remember to copy them a third time.
        """
        parsed = urlparse(self.path)
        try:
            route(parsed)
        except (BrokenPipeError, ConnectionResetError):
            self._log_disconnect(parsed.path)
        except Exception as e:
            logger.exception("Request failed: %s", parsed.path)
            self._send_error_response(parsed.path, e)

    def _route_get(self, parsed) -> None:
        query = parse_qs(parsed.query)
        if parsed.path == "/api/status":
            self._send_json(_status(_cfg, _vault))
        elif parsed.path == "/api/clients":
            from .client_tracking import list_connected_clients
            self._send_json({"clients": list_connected_clients()})
        elif parsed.path == "/api/mcp/windows":
            # The MCP servers the *LLM provider's* client has mounted — i.e. what
            # Claude can reach. Distinct from /api/clients, which lists the client
            # surfaces attached to this delegation-core process. Both are "MCP
            # connections"; they point in opposite directions.
            from .windows import list_windows
            self._send_json(list_windows())
        elif parsed.path == "/api/vault/notes":
            self._handle_vault_notes(query)
        elif parsed.path == "/api/vault/backlinks":
            self._handle_vault_backlinks(query)
        elif parsed.path == "/api/vault/find":
            self._handle_vault_find(query)
        elif parsed.path == "/api/vault/tree":
            self._handle_vault_tree()
        elif parsed.path == "/api/vault/note":
            self._handle_vault_note(query)
        elif parsed.path == "/api/vault/search":
            self._handle_vault_search(query)
        elif parsed.path == "/api/vault/graph":
            # Defaults to excluding generated articles, which is what
            # _build_vault_graph's own signature, its docstring and the
            # frontend all already assumed. This line said ["1"] and was the
            # only one that disagreed, and since the frontend sends no
            # `generated` param it decided every graph the dashboard drew.
            #
            # The effect was not subtle. Nodes are capped at the 1500 most
            # recent, and 3427 of this vault's 3629 notes are graph_build
            # articles — so the cap filled with generated articles, which
            # carry no wikilinks between them, and crowded out the
            # hand-written notes that hold all of them. Measured on the live
            # vault: 238 nodes / 2962 edges excluded, against 1500 nodes /
            # **13** edges included. The dashboard rendered the latter: a
            # near-empty canvas, which is what sent us looking.
            include_generated = (query.get("generated") or ["0"])[0] != "0"
            self._send_json(_build_vault_graph(_cfg, include_generated=include_generated))
        elif parsed.path == "/api/graphs":
            from . import graphbridge
            self._send_json(graphbridge.list_graphs(_cfg))
        elif parsed.path == "/api/graphs/get":
            self._handle_graphs_get(query)
        elif parsed.path == "/api/graphs/affected":
            self._handle_graphs_affected(query)
        elif parsed.path == "/api/processes":
            self._handle_processes_list(query)
        elif parsed.path == "/api/processes/get":
            self._handle_processes_get(query)
        elif parsed.path == "/api/config":
            self._handle_config_get()
        else:
            self._send_json({"error": f"not found: {parsed.path}"}, status=404)

    def _log_disconnect(self, path: str) -> None:
        """A client that hung up is not an error, and must not be answered.

        The old handler treated it as one: a disconnect during the response
        raised out of _send_json, was logged with a full traceback at ERROR,
        and then the handler tried to send a 500 — on the same dead socket, so
        it raised again, this time out of do_GET entirely and into
        socketserver's "Exception occurred during processing of request".
        Two tracebacks and a scary log line for a client pressing Escape.

        Cheap to ignore under the old sidecar, whose stderr nobody read. These
        handlers now run inside the daemon, so this lands in the journal
        alongside real faults — and a 2-second curl against /api/status is
        enough to produce it, since that route waits on a llama.cpp health
        check.
        """
        logger.debug("client disconnected before the response was sent: %s", path)

    def _send_error_response(self, path: str, exc: Exception) -> None:
        """Send the 500 for a failed request, unless the client is already gone."""
        try:
            self._send_json({"error": str(exc)}, status=500)
        except (BrokenPipeError, ConnectionResetError):
            self._log_disconnect(path)

    _MAX_BODY_BYTES = 1_000_000  # 1 MB — generous for a process create/update payload

    #: Cap on how much of a rejected body we are willing to read just to answer
    #: cleanly. Past this the client is not making an honest mistake, and a reset
    #: is the right outcome rather than reading whatever it wants to send.
    _MAX_DRAIN_BYTES = 8_000_000

    def _drain_body(self, length: int) -> None:
        """Consume a body we are about to reject, so the response is readable."""
        remaining = min(length, self._MAX_DRAIN_BYTES)
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _read_json_body(self) -> dict | None:
        """Parse the request body as JSON. Returns None (and has already sent an
        error response) if the body is missing, oversized, or not valid JSON —
        callers should return immediately when this returns None."""
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            self._send_json({"error": "missing request body"}, status=400)
            return None
        if length > self._MAX_BODY_BYTES:
            # Answer, but read the body first. Closing with unread bytes still in
            # the socket makes the client see a connection reset instead of the
            # 413 — the rejection arrives as a transport failure, which is the
            # one outcome an explicit status code exists to avoid. It also made
            # the test for this flaky: it passed alone and failed under the load
            # of the full suite, where the timing tips the other way.
            self._drain_body(length)
            self._send_json({"error": "request body too large"}, status=413)
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            self._send_json({"error": f"invalid JSON body: {e}"}, status=400)
            return None
        if not isinstance(data, dict):
            self._send_json({"error": "request body must be a JSON object"}, status=400)
            return None
        return data

    def _route_post(self, parsed) -> None:
        if parsed.path == "/api/vault/note/rename":
            self._handle_note_rename()
        elif parsed.path == "/api/vault/note/create":
            self._handle_note_create()
        elif parsed.path == "/api/vault/note/save":
            self._handle_note_save()
        elif parsed.path == "/api/processes/create":
            self._handle_processes_create()
        elif parsed.path == "/api/processes/update":
            self._handle_processes_update()
        elif parsed.path == "/api/llama/start":
            self._handle_llama_start()
        elif parsed.path == "/api/llama/stop":
            self._handle_llama_stop()
        elif parsed.path == "/api/config/update":
            self._handle_config_update()
        elif parsed.path == "/api/system/purge_orphans":
            self._handle_purge_orphans()
        else:
            self._send_json({"error": f"not found: {parsed.path}"}, status=404)

    def _handle_processes_list(self, query) -> None:
        status = (query.get("status") or ["active"])[0]
        proc_query = (query.get("query") or [""])[0]
        processes = _tracker.list_processes(status=status, query=proc_query)
        self._send_json({"count": len(processes), "processes": processes})

    def _handle_processes_get(self, query) -> None:
        process_id = (query.get("id") or [""])[0]
        if not process_id:
            self._send_json({"error": "missing id parameter"}, status=400)
            return
        proc = _tracker.get(process_id)
        if proc is None:
            self._send_json({"error": f"not found: {process_id}"}, status=404)
            return
        self._send_json(proc)

    def _handle_graphs_get(self, query) -> None:
        name = (query.get("name") or [""])[0]
        if not name:
            self._send_json({"error": "missing name parameter"}, status=400)
            return
        from . import graphbridge
        res = graphbridge.get_report(_cfg, name)
        status_code = 404 if "error" in res else 200
        self._send_json(res, status=status_code)

    def _handle_graphs_affected(self, query) -> None:
        name = (query.get("name") or [""])[0]
        q = (query.get("query") or [""])[0]
        depth_str = (query.get("depth") or ["2"])[0]
        try:
            depth = int(depth_str)
        except ValueError:
            depth = 2
        if not name or not q:
            self._send_json({"error": "missing name or query parameter"}, status=400)
            return
        from . import graphbridge
        res = graphbridge.get_affected(_cfg, name, query=q, depth=depth)
        status_code = 404 if "error" in res else 200
        self._send_json(res, status=status_code)

    def _handle_config_get(self) -> None:
        config_dict = _config_publico(_cfg)
        models_dir = _cfg.models_dir
        available_models = []
        if models_dir.exists():
            for f in sorted(models_dir.glob("*.gguf")):
                available_models.append(str(f))
        self._send_json({
            "config": config_dict,
            "available_models": available_models
        })

    def _handle_config_update(self) -> None:
        data = self._read_json_body()
        if data is None:
            return

        if "engine_mode" in data:
            mode = str(data["engine_mode"]).strip().lower()
            if mode in ("local", "agent", "hybrid"):
                _cfg.engine_mode = mode

        if "budget_mode" in data:
            b_mode = str(data["budget_mode"]).strip().lower()
            if b_mode in ("normal", "cpu", "auto"):
                _cfg.budget_mode = b_mode

        if "llama_model" in data:
            _cfg.llama_model = str(data["llama_model"]).strip()

        if "llama_binary" in data:
            _cfg.llama_binary = str(data["llama_binary"]).strip()

        if "bge_model" in data:
            _cfg.bge_model = str(data["bge_model"]).strip()

        if "search_threshold" in data:
            try:
                _cfg.search_threshold = float(data["search_threshold"])
            except (ValueError, TypeError):
                pass

        if "merge_threshold" in data:
            try:
                _cfg.merge_threshold = float(data["merge_threshold"])
            except (ValueError, TypeError):
                pass

        if "max_tokens" in data:
            try:
                _cfg.max_tokens = int(data["max_tokens"])
            except (ValueError, TypeError):
                pass

        if "synthesis_enabled" in data:
            _cfg.synthesis_enabled = bool(data["synthesis_enabled"])

        if "synthesis_lang" in data:
            lang = str(data["synthesis_lang"]).strip().lower()
            if lang in ("en", "pt"):
                _cfg.synthesis_lang = lang

        if "web_search_enabled" in data:
            _cfg.web_search_enabled = bool(data["web_search_enabled"])

        if "llama_ctx" in data:
            try:
                _cfg.llama_ctx = int(data["llama_ctx"])
            except (ValueError, TypeError):
                pass

        if "llama_device" in data:
            valor = str(data["llama_device"]).strip().lower()
            if valor in DISPOSITIVOS_DO_MODELO:
                _cfg.llama_device = valor

        if "embed_device" in data:
            valor = str(data["embed_device"]).strip().lower()
            if valor in ("auto", "cpu", "cuda", "mps"):
                _cfg.embed_device = valor

        if "llama_ngl" in data:
            try:
                _cfg.llama_ngl = int(data["llama_ngl"])
            except (ValueError, TypeError):
                pass

        _cfg.save()
        from dataclasses import asdict
        self._send_json({"ok": True, "config": asdict(_cfg)})

    def _handle_purge_orphans(self) -> None:
        import os
        import psutil
        from .client_tracking import SESSIONS_DIR

        purged_sessions = 0
        if SESSIONS_DIR.exists():
            for f in SESSIONS_DIR.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    pid = data.get("pid")
                    if pid and not psutil.pid_exists(pid):
                        f.unlink(missing_ok=True)
                        purged_sessions += 1
                except Exception:
                    pass

        killed_orphans = 0
        my_pid = os.getpid()
        for proc in psutil.process_iter(["pid", "ppid", "cmdline"]):
            try:
                if proc.info["pid"] == my_pid:
                    continue
                cmdline = proc.info["cmdline"] or []
                if any("dashboard_api" in arg for arg in cmdline):
                    ppid = proc.info["ppid"]
                    if ppid == 1 or not psutil.pid_exists(ppid):
                        proc.kill()
                        killed_orphans += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        self._send_json({
            "ok": True,
            "purged_sessions": purged_sessions,
            "killed_orphans": killed_orphans,
            "message": f"Purged {purged_sessions} dead session files and killed {killed_orphans} orphaned sidecar processes."
        })

    def _handle_note_create(self) -> None:
        """Create a note through the same code path the MCP write_note uses.

        Both call notewriter.create_note rather than each writing files and
        indexing on their own — a second write path would be free to drift from
        the first, which is the failure mode this codebase has been removing.
        """
        data = self._read_json_body()
        if data is None:
            return
        result = _notewriter.create_note(
            _vault,
            str(data.get("folder", "")).strip(),
            str(data.get("title", "")).strip(),
            str(data.get("content", "")),
        )
        self._send_json(result, status=400 if "error" in result else 200)

    def _handle_note_rename(self) -> None:
        """Rename a note and repoint every wikilink aimed at it.

        Renaming without the rewrite is silent corruption — this branch broke
        two of its own links that way before the operation existed.
        """
        data = self._read_json_body()
        if data is None:
            return
        rel = str(data.get("path", "")).strip()
        title = str(data.get("new_title", "")).strip()
        if not rel or not title:
            self._send_json({"error": "path and new_title are required"}, status=400)
            return
        result = _notewriter.rename_note(_vault, rel, title)
        self._send_json(result, status=400 if "error" in result else 200)

    def _handle_note_save(self) -> None:
        """Overwrite an existing note's raw text and reindex it."""
        data = self._read_json_body()
        if data is None:
            return
        rel = str(data.get("path", "")).strip()
        if not rel:
            self._send_json({"error": "path is required"}, status=400)
            return
        if "content" not in data:
            self._send_json({"error": "content is required"}, status=400)
            return
        result = _notewriter.save_note(_vault, rel, str(data["content"]))
        self._send_json(result, status=400 if "error" in result else 200)

    def _handle_processes_create(self) -> None:
        data = self._read_json_body()
        if data is None:
            return
        name = str(data.get("name", "")).strip()
        if not name:
            self._send_json({"error": "name is required"}, status=400)
            return
        description = str(data.get("description", ""))
        steps = data.get("steps") or []
        if not isinstance(steps, list) or not all(isinstance(s, str) for s in steps):
            self._send_json({"error": "steps must be a list of strings"}, status=400)
            return
        proc = _tracker.create(name=name, description=description, steps=steps)
        self._send_json(proc, status=201)

    def _handle_processes_update(self) -> None:
        data = self._read_json_body()
        if data is None:
            return
        process_id = str(data.get("process_id", "")).strip()
        if not process_id:
            self._send_json({"error": "process_id is required"}, status=400)
            return
        from .tracker import VALID_STATUSES
        status = data.get("status", "")
        # isinstance guard before the `in` check: VALID_STATUSES is a set, and
        # `x not in a_set` raises TypeError for an unhashable x (e.g. a JSON
        # array/object sent as "status") rather than just failing validation.
        if status and (not isinstance(status, str) or status not in VALID_STATUSES):
            self._send_json(
                {"error": f"invalid status '{status}'. Valid: {sorted(VALID_STATUSES)}"},
                status=400,
            )
            return
        step_done = data.get("step_done", -1)
        # bool is a subclass of int in Python, so isinstance(True, int) is True —
        # explicitly exclude it so a JSON `true`/`false` (easy client-side typo
        # for "mark this done") doesn't silently become step_done=1/0 instead of
        # a clear validation error.
        if isinstance(step_done, bool) or not isinstance(step_done, int):
            self._send_json({"error": "step_done must be an integer"}, status=400)
            return
        proc = _tracker.update(
            process_id=process_id, note=str(data.get("note", "")),
            step_done=step_done, status=status,
        )
        if proc is None:
            self._send_json({"error": f"not found: {process_id}"}, status=404)
            return
        self._send_json(proc)

    def _handle_llama_start(self) -> None:
        # _start_locked() can block up to ~90s polling for health (see
        # engine.py) — run it in a background thread so this request returns
        # immediately; the frontend already polls /api/status every 5s and
        # will see llama_state flip to "online" once it's actually up.
        engine = _get_engine()
        threading.Thread(target=engine._start_locked, daemon=True).start()
        self._send_json({"status": "starting"})

    def _handle_llama_stop(self) -> None:
        proc = _find_llama_process()
        if proc is None:
            self._send_json({"status": "not_running"})
            return
        import psutil
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except psutil.TimeoutExpired:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
        self._send_json({"status": "stopped"})

    def _handle_vault_tree(self) -> None:
        """Directory shape only — notes come from /api/vault/notes per directory.

        The old version returned the newest 1000 notes of every top-level folder
        in one payload and no hierarchy at all, so 3661 notes sitting three
        levels down were unreachable. Directories are cheap to enumerate (25 in
        this vault), and paging notes per directory is what makes a 2711-note
        folder browsable.
        """
        self._send_json({"directories": _vault.list_directories()})

    def _handle_vault_notes(self, query) -> None:
        dir_rel = (query.get("dir") or [""])[0]
        if not dir_rel:
            self._send_json({"error": "dir is required"}, status=400)
            return
        try:
            offset = max(int((query.get("offset") or ["0"])[0]), 0)
            limit = min(max(int((query.get("limit") or ["200"])[0]), 1), 500)
        except ValueError:
            self._send_json({"error": "offset and limit must be integers"}, status=400)
            return
        result = _vault.list_notes_in(dir_rel, offset=offset, limit=limit)
        self._send_json(result, status=400 if "error" in result else 200)

    def _handle_vault_backlinks(self, query) -> None:
        """Which notes point at this one, and where this one points.

        The relation was always computed (linker.inject_backlinks writes it into
        note bodies on every write) but never exposed, so a reader saw only
        whatever text happened to be in the note.
        """
        rel = (query.get("path") or [""])[0]
        if not rel:
            self._send_json({"error": "path is required"}, status=400)
            return
        result = _vault.note_links(rel)
        self._send_json(result, status=400 if "error" in result else 200)

    def _handle_vault_find(self, query) -> None:
        """Literal title/path lookup — deliberately not the semantic search.

        /api/vault/search is BGE with a similarity cutoff, which cannot reliably
        answer "open the note called X": the exact title of a note written
        minutes earlier did not come back in its top 3.
        """
        q = (query.get("q") or [""])[0].strip()
        if not q:
            self._send_json({"error": "q is required"}, status=400)
            return
        try:
            limit = min(max(int((query.get("limit") or ["30"])[0]), 1), 100)
        except ValueError:
            self._send_json({"error": "limit must be an integer"}, status=400)
            return
        results = _vault.find_notes(q, limit=limit)
        self._send_json({"query": q, "count": len(results), "results": results})

    def _handle_vault_note(self, query) -> None:
        rel_path = (query.get("path") or [""])[0]
        if not rel_path:
            self._send_json({"error": "missing path parameter"}, status=400)
            return
        vault_root = _cfg.vault.resolve()
        target = (vault_root / rel_path).resolve()
        # NOT str(target).startswith(str(vault_root)) — a plain string prefix
        # check is bypassable whenever a sibling directory's name happens to
        # start with the vault root's own name (vault at .../vault, and e.g.
        # .../vault-secrets exists: "../vault-secrets/x" resolves outside the
        # vault but the string still starts with ".../vault"). relative_to()
        # only succeeds for a genuine path-component match.
        try:
            target.relative_to(vault_root)
        except ValueError:
            self._send_json({"error": "path escapes vault root"}, status=400)
            return
        if not target.exists():
            self._send_json({"error": f"not found: {rel_path}"}, status=404)
            return
        self._send_json({"path": rel_path, "content": target.read_text(encoding="utf-8")})

    def _handle_vault_search(self, query) -> None:
        q = (query.get("q") or [""])[0]
        limit = int((query.get("limit") or ["5"])[0])
        if not q:
            self._send_json({"error": "missing q parameter"}, status=400)
            return
        # Same default as the MCP tool: this vault is 95% generated articles,
        # so an unscoped search buries the user's own notes.
        scope = (query.get("scope") or ["notes"])[0]
        self._send_json({"query": q, "scope": scope,
                         "results": _vault.search(q, limit=limit, scope=scope)})


def _start_parent_watchdog(parent_pid: int, server: ThreadingHTTPServer) -> None:
    """Exit this process when the parent (Tauri) process dies.

    The Tauri backend kills this sidecar on clean window close, but nothing
    fires that path on a hard kill of the app — the orphaned sidecar then holds
    the BGE model's GPU memory until someone notices. psutil.Process pins
    create_time at construction, so a recycled PID is correctly seen as "not my
    parent anymore" (a bare pid_exists() poll would be fooled by PID reuse).
    """
    import os
    import time
    import psutil

    try:
        parent = psutil.Process(parent_pid)
    except (psutil.NoSuchProcess, ValueError):
        parent = None  # already gone (or nonsense pid) — shut down immediately

    def _watch():
        while parent is not None:
            try:
                # is_running() is True for zombies (parent killed but not yet
                # reaped by *its* parent), which is just as dead for our purposes.
                if not parent.is_running() or parent.status() == psutil.STATUS_ZOMBIE:
                    break
            except psutil.Error:
                break
            time.sleep(2.0)
        logger.info("parent process %s is gone — shutting down", parent_pid)
        # Graceful where cheap: unblock serve_forever() so the main thread runs
        # server_close() and exits normally. Called from a throwaway thread
        # because shutdown() blocks until the serve loop notices, and the
        # os._exit() backstop below must not depend on it ever returning.
        threading.Thread(target=server.shutdown, daemon=True).start()
        time.sleep(5.0)
        sys.stderr.flush()
        os._exit(0)  # backstop: main thread didn't exit (wedged handler etc.)

    threading.Thread(target=_watch, daemon=True, name="parent-watchdog").start()


def serve_in_process(cfg, vault, tracker, host: str = "127.0.0.1",
                     port: int = 0) -> ThreadingHTTPServer:
    """Serve these same routes from inside the daemon, on the daemon's objects.

    The handlers below read `_cfg`/`_vault`/`_tracker` as module globals, so the
    only difference between this and run() is where those three come from: run()
    builds its own, and this one is handed the daemon's already-warm instances.
    That is the whole point — a VaultManager is a resident copy of BGE-m3 (2314
    MiB on this machine, measured) plus a writer against ChromaDB, and the
    dashboard was opening a second one of each while the daemon held the first.

    Returns the server so the caller can shut it down; serving happens on a
    daemon thread, since the caller's main thread goes on to run the MCP
    transport. Binding failures propagate rather than being swallowed: a port
    already in use usually means a second daemon, which is worth failing loudly
    over.
    """
    global _cfg, _vault, _tracker
    _cfg, _vault, _tracker = cfg, vault, tracker

    server = ThreadingHTTPServer((host, port), _Handler)
    threading.Thread(
        target=server.serve_forever,
        daemon=True,
        name="dashboard-api",
    ).start()
    logger.info("dashboard API listening on http://%s:%d", host, server.server_address[1])
    return server


def run(port: int = 0, host: str = "127.0.0.1", parent_pid: int | None = None) -> None:
    from .config import Config

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        stream=sys.stderr)

    global _cfg, _vault, _tracker
    _cfg = Config.load()

    # Matches cli.py's cmd_run(): skip the HF Hub round-trip once the weights are
    # cached. Deliberately after Config.load(), because the decision depends on
    # which model is configured — and it must stay conditional, or a machine that
    # never downloaded the model can never start.
    from .embeddings import prefer_offline
    prefer_offline(getattr(_cfg, "bge_model", ""))
    if not _cfg.is_configured():
        sys.stderr.write("delegation-core is not configured.\nRun: delegation-core setup\n")
        sys.exit(1)

    from .vault import VaultManager
    _vault = VaultManager(_cfg)
    _vault._init()  # blocking — BGE + ChromaDB ready before serving, once, not per-request

    from .tracker import ProcessTracker
    _tracker = ProcessTracker(_cfg.processes_path)

    # Bind directly instead of test-binding a throwaway socket to pick a port
    # and then binding a second socket at that number for the real server —
    # that TOCTOU gap lets another process grab the port in between and crash
    # startup. ThreadingHTTPServer's own bind is the only bind that matters.
    server = ThreadingHTTPServer((host, port), _Handler)
    actual_port = server.server_address[1]
    if parent_pid is not None:
        _start_parent_watchdog(parent_pid, server)
    # Printed as the first line so a spawning parent (the Tauri sidecar) can
    # read it off stdout to learn which port got assigned when port=0.
    print(f"dashboard_api listening on http://{host}:{actual_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="delegation-core dashboard API (local sidecar)")
    parser.add_argument("--port", type=int, default=0, help="Port to bind (0 = pick a free one)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--parent-pid", type=int, default=None,
                        help="Exit automatically when this PID dies (passed by the Tauri app)")
    args = parser.parse_args()
    run(port=args.port, host=args.host, parent_pid=args.parent_pid)
