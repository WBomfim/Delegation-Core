"""
server.py: FastMCP tool definitions for delegation-core.
Called by run_server(); never run directly.

The tool surface is deliberately NOT listed here. This header used to carry a
hand-maintained inventory ("32 tools across seven groups") that read 32 while
the server served 47 — the same drift that made a comparable project's guide
wrong on four of four constants. Ask the running server instead: the
`capabilities()` tool reports the live list from `mcp.list_tools()`, plus which
graph exporters are wired and which are deliberately not.

v0.4 changes:
  - engine.invoke() is now async (httpx.AsyncClient); all run_in_executor
    wrappers for engine calls have been removed
  - run_maintenance / run_maintenance_bg use async organizer.run()
  - search_web tool added (DuckDuckGo + llama.cpp compression)
  - per-task token budgets apply in all modes (not only cpu mode)
  - write_note, vault_update_note, export_session now inject wikilinks +
    backlinks after every write via _post_write_links() (BGE-only, no llama.cpp)
  - vault_health added to heartbeat(); auto-calibration via budget_mode = "auto"
  - run_maintenance includes heal pass (sync + bg)

v0.7.0 changes:
  - graph_build/graph_list/graph_report added: a code-graph pipeline vendored
    from Graphify (github.com/Graphify-Labs/graphify — core pipeline + all
    language extractors; see delegation_core/graph/__init__.py for the
    include/exclude list). Opt-in via the [graph] extra. graph_build routes its
    GRAPH_REPORT.md through the existing inbox/organizer.run() pipeline instead
    of a separate note-writing path.

v0.7.1 changes:
  - graph_build also emits callflow.html (Mermaid architecture diagrams,
    vendored from Graphify's callflow_html.py) and wiki/ articles (one
    per-community/god-node note instead of one big report, vendored from
    Graphify's wiki.py) — both filed into the vault alongside GRAPH_REPORT.md.
  - graph_affected added: blast-radius query (vendored from Graphify's
    affected.py) — what else is affected if a file/symbol changes.
  - graph_hook_install/uninstall/status added: a git post-commit hook that
    keeps a graph's on-disk artifacts fresh after every commit (code-only, no
    LLM, no vault filing — see graph_hook.py's docstring for why this is a
    from-scratch delegation-core-native adaptation rather than a vendor of
    Graphify's hooks.py/watch.py, which are built around a different
    distribution model).

v0.7.2 changes:
  - graph_build no longer routes its report/wiki articles through _inbox/ +
    organizer.run(). That queued every wiki article (often 100+) through the
    full classify/synthesize LLM pipeline meant for messy raw documents —
    observed directly to turn one graph_build into 30+ minutes of sequential
    llama.cpp calls on already-clean, template-generated markdown that needed
    no rewriting. graphbridge.py now writes artifacts to the vault directly
    (file + index + BGE-only wikilinks, no LLM, no engine dependency).
  - cli.py extended: search/compress/note (write/read/update/list/find-similar)/
    graph (build/list/report/affected/hook install|uninstall|status)/process
    (create/list/update/get) — previously these were MCP-only despite this
    repo already having a real installed CLI (setup/run/status/reindex/
    maintain/ingest/relink) that just didn't cover the knowledge-work tools.

v0.8.0 changes:
  - ClientTrackingMiddleware (client_tracking.py) registered via
    mcp.add_middleware(): writes ~/.delegation_core/sessions/<pid>.json on
    every request (client name/version from the MCP initialize handshake,
    tool-call count, last-active time), cleaned up on exit alongside the
    existing engine-close atexit hook. Feeds the Tauri dashboard's Connected
    Clients panel — each running instance is one MCP client surface (Claude
    Code, Claude Desktop, Codex, etc.), since delegation-core runs over stdio
    and there's no single shared connection to introspect otherwise.

v0.8.2 changes:
  - list_mcp_clients tool added: exposes client_tracking.list_connected_clients()
    (previously dashboard-only, via dashboard_api.py's /api/clients) directly as
    an MCP tool, so "what's connected right now" can be answered from within a
    session instead of only from the Tauri dashboard.

v0.8.1: relink_folder's path-containment check was `str(target).startswith(
str(vault_root))` — a plain string-prefix comparison, bypassable whenever a
sibling directory's name happens to start with the vault root's own name
(vault at .../vault, and .../vault-old exists: "../vault-old/x" resolves
outside the vault but the string still starts with ".../vault"). Found during
a review of dashboard_api.py's identical check (same bug, same fix: Path.
relative_to() instead of a string comparison).
"""

import asyncio
import atexit
import contextlib
import faulthandler
import functools
import json
import logging
import sys
import threading
from datetime import datetime
from pathlib import Path

from fastmcp import FastMCP

from . import graph_hook
from . import capabilities as _capabilities
from . import graphbridge
from . import jobs
from . import localqueue as _localqueue
from . import notewriter as _notewriter
from . import recuperacao
from .auth import LocalTokenAuth
from .client_tracking import ClientTrackingMiddleware as _ClientTrackingMiddleware
from .client_tracking import cleanup_own_session_file as _cleanup_own_session_file
from .client_tracking import current_client_name as _current_client_name
from .client_tracking import list_connected_clients as _list_connected_clients
from . import session as _session
from . import windows as _windows
from .config import Config, with_lang
from .engine import DelegationEngine
from .engine import queue_stats as _local_model_queue_stats
from .ingest import IngestManager
from .localworker import LocalTaskWorker
from .organizer import heal as _heal_notes
from .organizer import relink_folder as _relink_folder
from .organizer import run as _run_maintenance
from .tracker import ProcessTracker
from .vault import VaultManager, resolve_vault_folder


def _post_write_links(note_path: Path, rel_path: str, folder: str, stem: str) -> None:
    """Thin binding of notewriter.post_write_links to this module's vault.

    The body moved to notewriter.py when the dashboard gained editing, so both
    surfaces share one write path instead of two that can drift.
    """
    _notewriter.post_write_links(_vault, note_path, rel_path, folder, stem)


async def _full_maintenance_cycle(engine, vault) -> dict:
    """Inbox processing + heal pass. Used by both sync and bg maintenance tools."""
    results = await _run_maintenance(engine, vault)
    if vault.cfg.heal_per_run > 0:
        try:
            heal_result = await _heal_notes(engine, vault)
            results["healed"] = heal_result["healed"]
            results["heal_remaining"] = heal_result["remaining"]
        except Exception as e:
            logger.warning("Heal pass failed: %s", e)
    return results


logger = logging.getLogger("server")

_engine:  DelegationEngine | None = None
_vault:   VaultManager | None = None
_tracker: ProcessTracker | None = None
_ingest:  IngestManager | None = None

# ── loop watchdog ─────────────────────────────────────────────────────────────
#
# On 2026-09-26 and again on 2026-09-29 the Mac daemon stayed up with port 8787
# open and answered nothing: a thread stuck in chromadb, the loop behind it.
# launchd restarts only a process that exits, so it stayed that way for days.
#
# faulthandler's timer runs on a C thread that needs no GIL, which is the point:
# the stuck thread held the GIL, so no Python watchdog could have run. The loop
# re-arms it every few seconds; if the loop stops, the timer fires, writes every
# thread's stack to WATCHDOG_LOG (the diagnosis the Mac needed `sample` for),
# and exits 1, which both launchd (SuccessfulExit false) and systemd
# (Restart=on-failure) restart.

WATCHDOG_LOG = Path.home() / ".delegation_core" / "watchdog_tracebacks.log"
_watchdog_file = None


def _arm_watchdog(timeout: float) -> bool:
    """Arm (or re-arm) the exit timer. False when turned off or unavailable."""
    global _watchdog_file
    if not timeout or timeout <= 0:
        return False
    try:
        if _watchdog_file is None:
            WATCHDOG_LOG.parent.mkdir(parents=True, exist_ok=True)
            _watchdog_file = open(WATCHDOG_LOG, "a", encoding="utf-8")
        faulthandler.dump_traceback_later(timeout, exit=True, file=_watchdog_file)
        return True
    except Exception as e:  # pragma: no cover - platform without faulthandler timers
        logger.warning("loop watchdog unavailable: %s", e)
        return False


async def _rearm_watchdog_forever(timeout: float, interval: float):
    try:
        while _arm_watchdog(timeout):
            await asyncio.sleep(interval)
    finally:
        faulthandler.cancel_dump_traceback_later()


@contextlib.asynccontextmanager
async def _lifespan(_server):
    timeout = getattr(_engine.cfg, "loop_watchdog_sec", 0) if _engine else 0
    task = None
    if timeout and timeout > 0:
        task = asyncio.get_running_loop().create_task(
            _rearm_watchdog_forever(timeout, min(30.0, timeout / 4)))
        logger.info("loop watchdog on: exit after %ss without the event loop "
                    "(stacks to %s)", timeout, WATCHDOG_LOG)
    try:
        yield {}
    finally:
        if task:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


mcp = FastMCP("delegation-core", lifespan=_lifespan)


def _retomar_reconstrucao_na_partida() -> threading.Thread:
    """Espera o indice abrir e, havendo pedido de reconstrucao, a executa.

    O pedido nasce em `recuperacao.pos_em_quarentena`, chamado de dentro da
    abertura do indice, e sobrevive a reinicios: o daemon que cai no meio da
    reconstrucao a retoma na proxima partida. Numa thread porque a abertura
    carrega o BGE e o transporte tem que subir sem esperar por ela.

    Submetida na familia `vault_reindex`, e nao numa propria: um reindex pedido
    por hook durante a reconstrucao e absorvido por ela em vez de correr junto.
    """
    def _esperar_e_submeter():
        try:
            _vault._ensure_ready()
        except Exception as e:
            logger.warning("Indice nao abriu na partida (%s)", e)
        if recuperacao.reconstrucao_pendente():
            job_id = jobs.submit("vault_reindex:reconstrucao",
                                 recuperacao.reconstruir, _vault, _ingest)
            logger.warning("Reconstrucao do indice em andamento (job %s)", job_id)

    t = threading.Thread(target=_esperar_e_submeter, daemon=True,
                         name="reconstrucao-do-indice")
    t.start()
    return t

# Tools that only call synchronous code are plain `def`: fastmcp (>=3.4, the
# pinned floor) runs those in its threadpool. As `async def` they ran on the
# event loop, so one call stuck inside chromadb froze every other request, which
# is how the Mac daemon stopped answering on 2026-09-26.
#
# The loop used to serialise them for free. The ones that write keep that here,
# so moving them to threads adds concurrency only between reads.
_tool_write_lock = threading.Lock()


def _serialized(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _tool_write_lock:
            return fn(*args, **kwargs)
    return wrapper


# ── core ─────────────────────────────────────────────────────────────────────

def _confidence(top_sim: float, model_name: str) -> str:
    """Grade a top similarity against the calibration of the model that produced it.

    Cosine bands are model calibration, not universal constants — see
    embeddings.MODEL_PROFILES for the measurements behind each one.
    """
    from .embeddings import profile_for
    high, medium = profile_for(model_name)["confidence"]
    return "high" if top_sim >= high else "medium" if top_sim >= medium else "low"


def _vault_subfolder_error(folder: str) -> str | None:
    """Reject a folder argument that escapes the vault. None when it is safe.

    relative_to(), not a string prefix check — str(target).startswith(str(root))
    is bypassable whenever a sibling directory's name happens to start with the
    vault root's own name (vault at .../vault and .../vault-old also exists:
    "../vault-old/x" resolves outside the vault while the string still starts
    with ".../vault"). Found and fixed in dashboard_api.py's identical check
    first (2026-07-23 dashboard review); this is the same bug.

    One function because every entry point taking a folder has to make this
    check, and the one that forgets is a path-traversal hole rather than a bug
    with a wrong number in it.
    """
    vault_root = _vault.cfg.vault.resolve()
    try:
        (vault_root / folder).resolve().relative_to(vault_root)
    except ValueError:
        return f"Invalid folder path: {folder}"
    return None


def _default_scope() -> str:
    """The scope to search when the caller did not name one.

    An explicit `default_search_scope` in config.json wins. Otherwise it is
    decided from the vault's own composition: 'notes' once generated articles
    are the majority — there, an unscoped search is mostly answered by machine
    output and the user's own writing loses — and 'all' when they are not, since
    a vault whose authoritative material was ingested has nothing to gain from a
    filter that hides exactly that. Anything unreadable falls back to 'notes',
    the historical behaviour, so a broken health pass narrows the search rather
    than silently widening it.
    """
    try:
        configured = (getattr(_vault.cfg, "default_search_scope", "") or "").strip()
    except Exception:
        configured = ""
    if configured:
        return configured
    try:
        health = _vault.get_health_summary()
        total = health.get("total_notes") or 0
        generated = health.get("generated_notes") or 0
        return "notes" if total and generated > total * 0.5 else "all"
    except Exception:
        return "notes"


async def _run_or_queue(task_name: str, make_result) -> str:
    """Run local-model work inline, or hand back a job_id when the queue is busy.

    One daemon now fronts every client, so an interactive tool can find the local
    model already occupied by somebody else's request. Blocking would be the
    obvious thing and the wrong one: the client applies its own deadline to a
    tool call — mcp_timeout_sec defaults to 60 — and a caller third in line
    behind two long generations blows through it and reports a dead server.

    So: free queue, behave exactly as before (inline, same response shape). Busy
    queue, submit and return the queued envelope, which is the same
    {job_id, status} contract the _bg tools already use and task_status already
    understands, including its "this usually takes N seconds" hint.

    `make_result(engine)` must return the tool's FINAL response string, not a raw
    completion, so that task_status(job_id) yields something directly usable
    rather than a fragment the caller has to reassemble.
    """
    stats = _local_model_queue_stats()
    if stats["running"] < max(1, _engine.cfg.llama_queue_concurrency):
        return await make_result(_engine)

    async def _in_background():
        # Fresh engine per background loop, for the reason spelled out in
        # _bg_maintenance_wrapper: httpx transports are bound to the loop that
        # created them, and jobs.submit runs asyncio.run() on a new one.
        bg_engine = DelegationEngine(_engine.cfg)
        try:
            return await make_result(bg_engine)
        finally:
            await bg_engine.aclose()

    job_id = jobs.submit(task_name, asyncio.run, _in_background())
    return json.dumps({
        "job_id": job_id,
        "status": "queued",
        "task": task_name,
        "queue": stats,
        "message": (f"The local model is busy ({stats['running']} running, "
                    f"{stats['waiting']} waiting). Queued — call task_status(job_id) "
                    f"for the result."),
    })


@mcp.tool()
async def search_vault(query: str, limit: int = 5, use_local: bool = False,
                        scope: str = "", graph: str = "", snippet_chars: int = 0,
                        client: str = "") -> str:
    """
    CALL THIS FIRST before answering any question that could have prior context.
    Semantic search the Obsidian vault using BGE embeddings.
    In local mode the summary is written by llama.cpp; in agent/hybrid mode you
    receive the ranked 'sources' and synthesize yourself. Set use_local=true to
    force the local model to write the summary (hybrid mode).
    Cite 'sources' titles when referencing notes. Flat token cost regardless of vault size.

    scope narrows what is searched — 'notes' (hand-written only),
    'generated' (graph_build wiki articles), 'external' (ingest_folder'd files),
    'all' (everything). graph='<name>' restricts to one built code graph.
    Each hit carries its 'kind'.

    Leaving scope unset picks it per vault rather than shipping one answer for
    every machine. A vault carrying code graphs is mostly not the user's writing
    — one holds 3692 generated articles against 187 hand-written notes, and
    under scope='all' a search for the exact title of a note written minutes
    earlier returned two unrelated generated articles instead. But a vault whose
    authoritative material is ingested is the mirror image: a fixed 'notes'
    default hid 6,637 external files from the search path every agent uses, and
    3 of 4 probe queries returned a raw transcript where 'all' returned the
    authoritative document. So the default is 'notes' when generated articles
    dominate the vault and 'all' when they do not; set default_search_scope in
    config.json to pin one. Pass scope explicitly to override either way.
    Every response names the scope it used.

    client='<name>' narrows to one client, composing with scope rather than
    replacing it. Matching is normalised, so 'Nortex', 'nortex' and 'NORTEX' are
    one client. It reaches ingested files too, when client_path_roots is
    configured. Use it whenever a question is about one client and the vault
    holds several — unfiltered, a query for one client's retention metrics came
    back six-of-ten from another. Every hit carries its 'client', so an
    unfiltered search still shows when a filter was called for.
    vault_stats() lists the clients actually present in the index.
    snippet_chars caps snippet length (0 = default); lower it when you only need
    titles and paths, since in agent mode every snippet is spent from your context.
    """
    scope = scope or _default_scope()
    hits = await asyncio.to_thread(_vault.search, query, limit=limit, scope=scope,
                                   graph=graph, snippet_chars=snippet_chars or 800,
                                   client=client)
    # Stating the scope on every response, not only on a miss: a caller reading
    # three results has no way to tell a narrow search from an exhaustive one.
    scope_used = "generated" if graph else scope
    if not hits:
        empty = {"query": query, "summary": "No results above similarity threshold.",
                 "sources": [], "scope": scope_used}
        if scope_used != "all":
            empty["note"] = (f"Searched scope={scope_used!r}"
                             + (f", graph={graph!r}" if graph else "")
                             + " — retry with scope='all' to widen.")
        return json.dumps(empty)
    if "error" in hits[0]:
        return json.dumps(hits[0])

    top_sim    = max(h["similarity"] for h in hits)
    confidence = _confidence(top_sim, _vault.cfg.bge_model)

    snippet_len = snippet_chars or (300 if _engine.cfg.is_cpu_budget else 800)
    combined = "\n\n".join(f"[{h['title']}]\n{h['snippet'][:snippet_len]}" for h in hits[:5])

    # Route the summarization. agent/hybrid delegate to the calling Claude
    # (return raw ranked notes); local (or hybrid + use_local) runs llama.cpp.
    route = _engine.cfg.route(task="search_summary", input_chars=len(combined), use_local=use_local)
    if route in ("agent", "offer"):
        payload = {
            "query": query, "summary": None, "sources": hits, "mode": route,
            "scope": scope_used,
            "instruction": "No server-side summary — read the 'sources' snippets and "
                           "synthesize the answer yourself.",
            "quality": {"confidence": confidence, "top_similarity": top_sim,
                        "sources_found": len(hits), "output_empty": False},
        }
        if route == "offer":
            payload["est_tokens_if_agent"] = len(combined) // 4
            payload["instruction"] = ("Big result. Synthesize it yourself, or re-call with "
                                      "use_local=true to have the local model summarize it.")
        return json.dumps(payload)

    async def _summarize(engine) -> str:
        try:
            summary = await engine.invoke(
                f"Summarize these vault notes for the query: {query}\n\n{combined}",
                system=with_lang(
                    "Vault Analyst. Return compressed insight only — no preamble, no headers.",
                    engine.cfg),
                max_tokens=engine.budget("search_summary", 800),
                temperature=0.3,
                task="search_summary",
            )
        except Exception as e:
            logger.warning("search_vault: llama.cpp summarization failed (%s) — returning raw hits", e)
            return json.dumps({
                "query": query, "summary": None, "sources": hits, "degraded": True,
                "scope": scope_used,
                "note": "llama.cpp offline — returning raw snippets without summarization.",
                "quality": {"confidence": confidence, "top_similarity": top_sim,
                            "sources_found": len(hits), "output_empty": True},
            })

        output_empty = not summary or len(summary.strip()) < 20
        return json.dumps({
            "query": query, "summary": summary, "sources": hits, "scope": scope_used,
            "quality": {"confidence": confidence, "top_similarity": top_sim,
                        "sources_found": len(hits), "output_empty": output_empty},
        })

    return await _run_or_queue("search_summary", _summarize)


@mcp.tool()
def read_note(note_name: str) -> str:
    """
    Read the full content of one specific vault note by filename stem (partial, case-insensitive).
    Use only when you need the complete text of a known note.
    For discovery or topic recall, use search_vault instead.
    """
    matches = _vault.find_notes_by_stem(note_name)
    if not matches:
        return json.dumps({"error": f"Note not found: {note_name}"})
    try:
        return matches[0].read_text(encoding="utf-8")
    except OSError as e:
        return json.dumps({"error": f"Could not read note: {e}"})


@mcp.tool()
@_serialized
def write_note(folder: str, title: str, content: str,
                     ai_generated: bool = True) -> str:
    """
    Persist information to the vault and index it immediately with BGE embeddings.
    CALL THIS automatically after: any decision, meeting summary, research finding,
    fix, or reusable tool/prompt. Write proactively — don't wait to be asked.
    Use vault_update_note when adding to an existing topic.
    folder must be one of the configured vault_folders.

    ai_generated stamps the note's frontmatter and defaults to true, which is
    right for you: you are an agent, and you wrote it. It exists because the CLI
    `note write` carries a person's text from stdin and has to say so, and
    because that command now routes here instead of opening ChromaDB as a
    second writer alongside this daemon.
    """
    result = _notewriter.create_note(_vault, folder, title, content,
                                     ai_generated=ai_generated)
    if "error" in result:
        return json.dumps(result)
    # Historic response shape: bare filename, not the vault-relative path.
    return json.dumps({"status": "ok", "path": result["name"], "folder": result["folder"]})


@mcp.tool()
async def compress(source: str, raw_content: str, use_local: bool = False) -> str:
    """
    CALL THIS whenever the user pastes a document, email, or content over ~500 words.
    Returns key facts, decisions, and action items.
    In agent/hybrid mode compression is delegated to the calling Claude; in local
    mode (or hybrid with use_local=true, or a big input) the local model does it.
    Max input: 6000 characters (1500 in CPU budget mode). Chunk longer content.
    """
    limit = 1500 if _engine.cfg.is_cpu_budget else 6000
    input_chars = len(raw_content)
    truncated   = input_chars > limit

    # Route: agent → hand raw text to Claude; offer → big input, surface the
    # local-model choice + cost; local → llama.cpp compresses it here.
    route = _engine.cfg.route(task="compress", input_chars=input_chars, use_local=use_local)
    if route in ("agent", "offer"):
        payload = {
            "source": source, "compressed": None, "mode": route,
            "raw_content": raw_content[:limit],
            "instruction": "Compress this raw_content yourself into key facts, "
                           "decisions, and action items — the server did not.",
            "quality": {"input_chars": input_chars, "truncated_input": truncated},
        }
        if route == "offer":
            payload["est_tokens_if_agent"] = input_chars // 4
            payload["instruction"] = (f"Big input (~{input_chars // 4} tokens). Compress it "
                                      "yourself, or re-call with use_local=true to offload it "
                                      "to the local model (slower, but zero agent tokens).")
        return json.dumps(payload)

    async def _compress(engine) -> str:
        # synthesis_lang is exposed in config and, until 03/09/2026, was read by
        # compression prompt was hardcoded English and never read it. The local
        # model then chose per call: two consecutive calls over Portuguese
        # conversations came back one in Portuguese and one in English. Across a
        # batch that produces a bilingual vault by accident, and nothing reports
        # it — the compression "succeeded" every time.
        try:
            result = await engine.invoke(
                f"Extract only key facts, decisions, and action items. No preamble.\n"
                f"Source: {source}\n\n{raw_content[:limit]}",
                system=with_lang("Compression Engine. Be extremely concise.", engine.cfg),
                max_tokens=engine.budget("compress", 1200),
                temperature=0.2,
                task="compress",
            )
        except Exception as e:
            return json.dumps({"error": f"Compression failed: {e}", "source": source})

        output_chars = len(result.strip()) if result else 0
        ratio = round(output_chars / max(input_chars, 1), 2)
        return json.dumps({
            "source": source, "compressed": result,
            "quality": {
                "input_chars": input_chars, "output_chars": output_chars,
                "ratio": ratio, "truncated_input": truncated,
                "poor": output_chars < 30 or ratio > 0.85,
            },
        })

    return await _run_or_queue("compress", _compress)


@mcp.tool()
async def vault_health_detail(limit: int = 50) -> str:
    """The findings behind heartbeat's vault_health counts, itemised.

    Use this instead of writing a script to enumerate broken links or orphans.
    A script re-implements the definitions — which link syntax counts, what a
    target may resolve to — and three such scripts here reported 248, 63 and 5
    against true values of 31, 31 and 0. These lists are collected in the same
    pass that produces the counts, so they cannot disagree with them.

    Returns broken_link_items (source, folder, target), orphan_items,
    needs_repair_items, truncated_items, and two buckets of links that are
    deliberately NOT counted as broken, listed so they are not "fixed" by
    mistake: folder_marker_items, the `[[reference]]`-style category markers,
    and ingested_link_items, links to files ingested from outside the vault,
    which resolve through search_vault(scope="external").
    """
    return json.dumps(await asyncio.to_thread(_vault.health_detail, limit))


@mcp.tool()
def vault_stats() -> str:
    """Return note counts per vault folder, ChromaDB index size, and embedding model info."""
    return json.dumps(_vault.get_stats())



def _dispositivos(cfg) -> dict:
    """Onde cada modelo foi configurado para rodar. O valor de config, e nao o
    resolvido: resolver "auto" importa o torch, caro demais para o heartbeat."""
    from .engine import llama_device
    return {"embeddings": (getattr(cfg, "embed_device", "auto") or "auto").strip().lower(),
            "modelo_local": llama_device(cfg)}


@mcp.tool()
async def heartbeat(force: bool = False) -> str:
    """
    CALL THIS at the start of every session before using any other tool.
    Returns llama.cpp status, vault stats, active background jobs, and configuration summary.
    If status is 'degraded', warn the user before proceeding.
    Pass force=true to bypass the 5-minute vault_health cache and recalculate live.
    """
    cfg = _engine.cfg
    # In agent mode there is no local model, so llama being "offline" is the
    # expected healthy state. In hybrid mode llama is started on demand for
    # big/bulk tasks, so "not running yet" is also healthy — only pure local
    # mode treats an unreachable llama as degraded.
    if cfg.is_agent_mode:
        status, llama_state = "healthy", "delegated-to-agent"
    elif cfg.is_hybrid_mode:
        llama_ok = await _engine.check_health()
        status = "healthy"
        llama_state = "online" if llama_ok else "on-demand (local for big/bulk tasks)"
    else:
        llama_ok = await _engine.check_health()
        status, llama_state = ("healthy" if llama_ok else "degraded",
                               "online" if llama_ok else "offline")
    stats, health, processes = await asyncio.to_thread(
        lambda: (_vault.get_stats(), _vault.get_health_summary(force=force),
                 _tracker.summary()))
    # Um indice sendo reconstruido responde, mas responde pouco: sem isto a
    # busca parece so pior, e nada diz que vai melhorar sozinha.
    pedido = recuperacao.reconstrucao_pendente()
    if pedido and status == "healthy":
        status = "degraded"
    # BGE ou ChromaDB que nao subiram: o servidor responde, a busca nao. Era
    # "healthy" ate 02/10/2026, quando um Windows bloqueou a DLL do PyTorch.
    erro_embeddings = getattr(_vault, "init_error", None)
    if erro_embeddings:
        status = "degraded"
    return json.dumps({
        "status":      status,
        "index_recovery": pedido and {
            "motivo": pedido.get("motivo"), "pedido_em": pedido.get("pedido_em"),
            "quarentena": pedido.get("quarentena"),
            "notas_feitas": pedido.get("notas_feitas"),
            "fontes": f"{len(pedido.get('fontes_feitas') or [])}/{len(pedido.get('fontes') or [])}",
            "detail": "the index crashed whoever opened it and was set aside; the "
                      "daemon is rebuilding it from the vault and the ingest sources, "
                      "and search fills in as it goes"},
        "embeddings":  ({"status": "unavailable", "error": erro_embeddings}
                        if erro_embeddings else {"status": "ok"}),
        "timestamp":   datetime.now().isoformat(),
        "engine_mode": cfg.engine_mode,
        "dispositivos": _dispositivos(cfg),
        "llama_cpp":   llama_state,
        "llama_url":   cfg.llama_url,
        "vault":       stats,
        "vault_health": health,
        "background_jobs": jobs.running_count(),
        "processes":   processes,
        "config": {
            "synthesis_enabled":  cfg.synthesis_enabled,
            "synthesis_lang":     cfg.synthesis_lang,
            "budget_mode":        cfg.budget_mode,
            "tok_sec":            cfg.tok_sec,
            "mcp_timeout_sec":    cfg.mcp_timeout_sec,
            "quality_threshold":  cfg.quality_threshold,
            "heal_per_run":       cfg.heal_per_run,
            "split_min_chars":    cfg.split_min_chars,
            "split_max_notes":    cfg.split_max_notes,
            "web_search_enabled": cfg.web_search_enabled,
            "hybrid_local_min_chars": cfg.hybrid_local_min_chars,
        },
    })


@mcp.tool()
async def window_list() -> str:
    """
    List MCP servers registered as windows, which are currently mounted in the
    client, and the active workspace.

    delegation-core does not connect to these servers or call their tools — it only
    curates which ones the client has mounted. A registered-but-unmounted server is
    configured and dormant: its tool schemas cost no context until reopened.
    Changes take effect when the client reconnects.
    """
    return json.dumps(_windows.list_windows())


@mcp.tool()
async def window_open(name: str) -> str:
    """
    Mount a registered MCP server into the client configuration.

    Use when the user needs a server's tools available. The server must already be
    known — servers are catalogued automatically from whatever the client has
    mounted, so anything used before can be reopened by name.
    Requires a client reconnect to take effect.
    """
    return json.dumps(_windows.open_window(name))


@mcp.tool()
async def window_close(name: str) -> str:
    """
    Unmount an MCP server from the client configuration, freeing the context its
    tool schemas occupy. The server's definition is kept, so window_open restores it
    exactly. delegation-core itself cannot be closed.
    Requires a client reconnect to take effect.
    """
    return json.dumps(_windows.close_window(name))


@mcp.tool()
async def workspace_list() -> str:
    """
    List saved workspaces — named sets of MCP servers — and which one is active.
    """
    return json.dumps(_windows.list_workspaces())


@mcp.tool()
async def workspace_save(name: str) -> str:
    """
    Save the currently mounted set of servers as a named workspace.

    Use after arranging the servers needed for a kind of work, so the arrangement
    can be restored later in one step (e.g. "soteria" = clickup; "dev" = github).
    """
    return json.dumps(_windows.save_workspace(name))


@mcp.tool()
async def workspace_apply(name: str) -> str:
    """
    Make the client's mounted servers match a named workspace.

    Servers outside the workspace are unmounted but keep their definitions and can
    be reopened. Only the top-level server set is rewritten; per-project definitions
    are never touched. Requires a client reconnect to take effect.
    """
    return json.dumps(_windows.apply_workspace(name))


@mcp.tool()
async def list_mcp_clients() -> str:
    """
    List MCP client surfaces currently connected to this delegation-core daemon
    (Claude Code, Claude Desktop, Codex, etc. — whichever has delegation-core
    configured and has been active in the last two minutes). One daemon serves
    every client, and each connection is its own MCP session with its own client
    name/version, first/last-active timestamps, and tool-call count. Use this to
    answer "what's connected right now" instead of guessing from config files.
    """
    return json.dumps({"clients": _list_connected_clients()})


@mcp.tool()
async def local_task_submit(prompt: str, system: str = "", task: str = "default",
                            run_after: str = "", note: str = "") -> str:
    """
    Queue work for the LOCAL model (llama.cpp) and return immediately with a task id.

    Use this instead of doing it yourself when the work is bulk, repetitive, or
    not worth your own tokens — summarising a long document, classifying a batch,
    drafting something you will rewrite anyway. You are one of several agents
    connected to this daemon; the local model is a single GPU slot serving all of
    you, so work is queued rather than called and the line is drained one task at
    a time in submission order.

    This does NOT block. Poll `local_task_status(task_id)` for the result, or
    leave it and come back — the queue is on disk and survives a daemon restart,
    so a task outlives the session that submitted it.

    run_after: ISO-8601 timestamp. The task waits until then instead of queueing
    now — that is how you schedule work for the local model (overnight batches,
    "do this after the ingest finishes"). Omit it to run as soon as the line
    clears.

    Note the local model runs even when engine_mode is "agent": a task queued
    here is an explicit request for llama.cpp, so it starts on demand. It shares
    the GPU with BGE-m3, so queue what is worth that, not everything.
    """
    try:
        record = _localqueue.submit(
            prompt=prompt, system=system, task=task,
            submitted_by=_current_client_name(), run_after=run_after, note=note,
        )
    except ValueError as e:
        return json.dumps({"error": str(e)})
    return json.dumps({
        "task_id": record["id"],
        "status": record["status"],
        "submitted_by": record["submitted_by"],
        "queue": _localqueue.stats(),
        "next": "Poll local_task_status(task_id); it does not block.",
    })


@mcp.tool()
async def local_task_status(task_id: str) -> str:
    """
    Check one queued local-model task: its state, and its result once finished.

    States: scheduled (waiting for run_after), queued, running, done, error,
    cancelled. `result` is populated only when done; `error` only when errored.
    """
    record = _localqueue.get(task_id)
    if record is None:
        return json.dumps({
            "error": f"No local task {task_id!r}.",
            "hint": "Finished tasks are pruned once the line grows past its cap; "
                    "an id from a while ago may simply have aged out.",
        })
    return json.dumps(record)


@mcp.tool()
async def local_task_list(status: str = "", limit: int = 20) -> str:
    """
    List local-model tasks, newest first, with who submitted each one.

    Filter by status ("queued", "running", "scheduled", "done", "error",
    "cancelled") or omit it for everything. Use this to see what the other
    connected agents have put in the line before adding to it.
    """
    return json.dumps({
        "queue": _localqueue.stats(),
        "tasks": _localqueue.list_tasks(status=status, limit=limit),
    })


@mcp.tool()
async def local_task_cancel(task_id: str) -> str:
    """
    Drop a local-model task that has not started yet.

    A running task is left alone and reported as running: stopping it would mean
    killing the llama.cpp process every other queued task is waiting on.
    """
    record = _localqueue.cancel(task_id)
    if record is None:
        return json.dumps({"error": f"No local task {task_id!r}."})
    return json.dumps(record)


@mcp.tool()
@_serialized
def export_session(title: str, summary: str, key_decisions: str = "") -> str:
    """
    Save a curated summary of this conversation to the vault's sessions/ folder.
    CALL THIS when the user signals they are ending the session — any variation of
    goodbye, thanks, we're done, wrapping up, see you tomorrow, etc.
    Do not wait to be asked. Fire proactively the moment you detect session-ending intent.
    """
    result = _session.export(_vault, title, summary, key_decisions)
    if result.get("status") == "ok" and result.get("path") and result.get("folder"):
        rel = f"{result['folder']}/{result['path']}"
        note_path = _vault.cfg.vault / rel
        if note_path.exists():
            _post_write_links(note_path, rel, result["folder"], note_path.stem)
    return json.dumps(result)


@mcp.tool()
async def run_maintenance() -> str:
    """
    Run vault maintenance synchronously. Classify inbox notes, merge near-duplicates,
    add wikilinks, write weekly summary, then heal low-quality notes.
    Use run_maintenance_bg for large inboxes.
    """
    # Routed through the same worker-thread path as run_maintenance_bg (fresh
    # DelegationEngine + its own event loop) rather than awaited directly on the
    # main loop. The pipeline underneath (extractor.extract, vault.search/
    # index_note per file) is synchronous, and FastMCP dispatches every incoming
    # request as its own concurrent task on this one event loop (mcp.server.
    # lowlevel.server.Server.run: tg.start_soon per message) — a client can
    # pipeline a heartbeat()/task_status() call alongside this one, and a
    # multi-file inbox pass would otherwise stall it until the whole cycle
    # finished. This keeps run_maintenance()'s own contract (blocks until the
    # full result is ready) while freeing the loop for everything else.
    results = await asyncio.to_thread(asyncio.run, _bg_maintenance_wrapper())
    return json.dumps(results)


# ── maintenance ───────────────────────────────────────────────────────────────

@mcp.tool()
def vault_list_notes(folder: str, limit: int = 20) -> str:
    """List notes in a vault folder sorted newest-first. Returns title, date, path, size.

    `count` is how many were returned; `total` is how many the folder holds.
    When `truncated` is true you are seeing only the newest `limit` — raise the
    limit or narrow with search_vault rather than concluding the folder is small.
    """
    resolved_folder = resolve_vault_folder(_vault.cfg, folder)
    if not resolved_folder:
        return json.dumps({"error": f"Invalid folder '{folder}'. Valid: {_vault.cfg.vault_folders}"})
    folder = resolved_folder
    notes = _vault.list_notes(folder, limit=limit)
    total = _vault.count_notes(folder)
    return json.dumps({"folder": folder, "count": len(notes), "total": total,
                       "truncated": total > len(notes), "notes": notes})


@mcp.tool()
def vault_find_notes(query: str, limit: int = 30) -> str:
    """Find notes by literal title or path match — no embeddings, no threshold.

    Use this when you know what a note is CALLED. search_vault is semantic and
    answers "what is about X"; it cannot reliably answer "open the note named X"
    — searching this vault for the exact title of a note written minutes earlier
    did not return it in the top 3. Results are ranked: exact stem, prefix,
    substring, then anywhere in the path.
    """
    results = _vault.find_notes(query, limit=limit)
    return json.dumps({"query": query, "count": len(results), "results": results})


@mcp.tool()
@_serialized
def vault_rename_note(path: str, new_title: str) -> str:
    """Rename a note and repoint every [[wikilink]] that referenced it.

    Renaming a note by any other means breaks its inbound links silently — a
    stem is the note's link identity. Section anchors and display text
    (`[[stem#Summary|label]]`) are preserved. Returns how many notes were
    rewritten. Staged and rolled back on failure, so a half-renamed vault is not
    a reachable state.
    """
    return json.dumps(_notewriter.rename_note(_vault, path, new_title))


@mcp.tool()
def vault_note_links(path: str) -> str:
    """Inbound and outbound wikilinks for one note, by vault-relative path.

    Answers "what references this note" without reading every candidate. Broken
    outbound links are returned with broken=true rather than dropped, so a note
    pointing at something that no longer exists is visible instead of silently
    looking well-connected.
    """
    return json.dumps(_vault.note_links(path))


@mcp.tool()
def vault_inbox_status() -> str:
    """Check what files are waiting in _inbox. Call BEFORE run_maintenance."""
    return json.dumps(_vault.inbox_status())


@mcp.tool()
def vault_find_similar(note_name: str, threshold: float = 0.80, limit: int = 5) -> str:
    """Find notes semantically similar to the given note. Useful before merging."""
    results = _vault.find_similar(note_name, threshold=threshold, limit=limit)
    return json.dumps({"source_note": note_name, "threshold": threshold, "similar": results})


@mcp.tool()
@_serialized
def vault_update_note(note_name: str, append_content: str) -> str:
    """Append content to an existing note and re-index. Prefer over write_note for follow-ups."""
    result = _vault.update_note(note_name, append_content)
    if "error" not in result:
        matches = _vault.find_notes_by_stem(note_name)
        if matches:
            f = matches[0]
            rel = f.relative_to(_vault.cfg.vault).as_posix()
            _post_write_links(f, rel, f.parent.name, f.stem)
    return json.dumps(result)


@mcp.tool()
async def relink_folder(
    folder: str,
    days: int | None = None,
    min_similarity: float | None = None,
    max_links_per_note: int = 8,
) -> str:
    """
    Additively add wikilinks under `## Related` for notes in a vault subfolder.
    Use after bulk ingestion or when a topic cluster should cross-link.
    Strictly additive — never removes existing wikilinks.
    folder: vault-relative subpath (e.g. 'meetings/Client/2026' or 'meetings')
    days: restrict to notes modified within last N days (None = all)
    """
    err = _vault_subfolder_error(folder)
    if err:
        return json.dumps({"error": err})
    loop = asyncio.get_running_loop()
    try:
        results = await loop.run_in_executor(
            None,
            lambda: _relink_folder(_vault, folder, days=days, min_similarity=min_similarity,
                                   max_links_per_note=max_links_per_note),
        )
    except Exception as e:
        return json.dumps({"error": f"relink_folder failed: {e}"})
    return json.dumps(results)


@mcp.tool()
async def relink_folder_bg(
    folder: str,
    days: int | None = None,
    min_similarity: float | None = None,
    max_links_per_note: int = 8,
) -> str:
    """Cross-link a vault subfolder in the background. Returns a job_id immediately.

    Prefer this over relink_folder on any folder of real size. The synchronous
    tool embeds and compares every note in the folder against every other, which
    on a 31-note folder ran past the MCP client's 300-second idle timeout: the
    call was aborted client-side and the connection dropped, while the server
    kept working and finished the job nobody was listening for any more. Work
    that legitimately outlasts the caller's patience needs a job id, not a
    longer wait — the same reason run_maintenance and ingest_folder have one.
    """
    err = _vault_subfolder_error(folder)
    if err:
        return json.dumps({"error": err})
    job_id = jobs.submit("relink_folder", _relink_folder, _vault, folder,
                         days=days, min_similarity=min_similarity,
                         max_links_per_note=max_links_per_note)
    return json.dumps({"job_id": job_id, "folder": folder, "status": "running",
                       "message": "Relink started. Call task_status(job_id) to check progress."})


@mcp.tool()
async def search_web(query: str, num_results: int = 5, use_local: bool = False) -> str:
    """
    Search the web via DuckDuckGo. The fetch always runs locally; the summary is
    written by llama.cpp in local mode, or delegated to the calling Claude in
    agent/hybrid mode (pass use_local=true to force the local model).
    Returns a JSON summary with sources.

    Opt-in feature (v5.1): disabled unless web_search_enabled=true in config.json
    AND the [web] extra is installed (pip install "delegation-core[web]").
    """
    # v5.1: gate on the config flag first so a default install advertises the
    # tool but refuses to reach the internet until the user explicitly opts in.
    if not _engine.cfg.web_search_enabled:
        return json.dumps({"error": "web search is disabled. Set web_search_enabled=true in "
                                    "~/.delegation_core/config.json and restart to enable."})
    # v5.1: the `duckduckgo-search` package was renamed to `ddgs` and the old
    # name now emits a deprecation warning and returns 0 results. Prefer `ddgs`,
    # fall back to the legacy import so older installs keep importing.
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            return json.dumps({"error": 'web search backend not installed. Run: pip install "delegation-core[web]"'})
    try:
        loop = asyncio.get_running_loop()
        raw_results = await loop.run_in_executor(
            None, lambda: list(DDGS().text(query, max_results=num_results))
        )
        if not raw_results:
            return json.dumps({"query": query, "summary": "No results found.", "sources": []})
        sources = [
            {"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")[:200]}
            for r in raw_results
        ]
        # Fetch stays local (DuckDuckGo). Route the summary: agent/hybrid hand
        # results to the calling Claude; local (or use_local) uses llama.cpp.
        snippets_len = sum(len(r.get("body", "")) for r in raw_results)
        route = _engine.cfg.route(task="compress", input_chars=snippets_len, use_local=use_local)
        if route in ("agent", "offer"):
            return json.dumps({
                "query": query, "summary": None, "sources": sources, "mode": route,
                "instruction": "Summarize these 'sources' yourself"
                               + (" (or re-call with use_local=true for the local model)."
                                  if route == "offer" else "."),
            })
        snippets = "\n\n".join(
            f"[{i+1}] {r.get('title', '')}\n{r.get('body', '')}"
            for i, r in enumerate(raw_results)
        )[:5000]

        async def _summarize(engine) -> str:
            summary = await engine.invoke(
                f"Compress these search results into key facts for: {query}\n\n{snippets}",
                system=with_lang("Research Compressor. Be extremely concise.", engine.cfg),
                max_tokens=engine.budget("compress", 400),
                temperature=0.2,
                task="compress",
            )
            return json.dumps({"query": query, "summary": summary, "sources": sources})

        return await _run_or_queue("search_web_summary", _summarize)
    except Exception as e:
        logger.error("search_web failed: %s", e)
        return json.dumps({"error": str(e)})


# ── fire-and-forget ───────────────────────────────────────────────────────────

async def _bg_maintenance_wrapper() -> dict:
    """Runs _full_maintenance_cycle with a fresh DelegationEngine for the background thread.

    asyncio.run() in jobs.submit creates a new event loop. httpx.AsyncClient transports
    are bound to the loop that created them — sharing _engine._http across loops raises
    RuntimeError on the first pooled connection. Creating bg_engine here ensures the
    client is born in the background loop and never touches the main loop's transports.
    """
    bg_engine = DelegationEngine(_engine.cfg)
    try:
        return await _full_maintenance_cycle(bg_engine, _vault)
    finally:
        await bg_engine.aclose()


@mcp.tool()
async def run_maintenance_bg() -> str:
    """Start vault maintenance (inbox + heal pass) in the background. Returns a job_id immediately."""
    job_id = jobs.submit("run_maintenance", asyncio.run, _bg_maintenance_wrapper())
    return json.dumps({"job_id": job_id, "status": "running",
                       "message": "Maintenance + heal pass started. Call task_status(job_id) to check progress."})


@mcp.tool()
async def vault_reindex_bg(force: bool = False) -> str:
    """Rebuild the ChromaDB index in the background. Returns a job_id immediately.
    force=False (default): incremental — only reindexes notes changed since last run.
    force=True: full reindex of every note.
    """
    import functools
    fn = functools.partial(_vault.reindex_vault, force=force)
    mode = "full" if force else "incremental"
    # Keyed by mode, not by task name alone. A full reindex and an incremental
    # one are different jobs by two orders of magnitude — measured here as 642.3s
    # against 1.7s — and sharing a bucket made the median describe neither. This
    # function already knew which one it was launching: `mode` is computed on the
    # line above and returned to the caller. It just was not used for the bucket.
    job_id = jobs.submit(f"vault_reindex:{mode}", fn)
    return json.dumps({"job_id": job_id, "status": "running", "mode": mode,
                       "message": f"{mode.capitalize()} reindex started. Call task_status(job_id) to check progress."})


@mcp.tool()
async def task_status(job_id: str) -> str:
    """Check the status of a background job.

    While running, also reports how long this kind of job usually takes
    (`typical_seconds`, median of the last completed runs) and when it is worth
    checking again (`check_again_in_seconds`). Without those, elapsed time alone
    cannot distinguish a job that is nearly done from one with minutes to go.
    Both are absent the first time a given task runs on this machine.
    """
    job = jobs.get(job_id)
    if not job:
        # Deliberately carries no "status" key: daemon.next_poll_wait() keys on
        # its absence to tell not-found from a real job state, and a "status"
        # here would read as neither done nor error and send the CLI back round
        # the poll loop until it timed out. The extra fields below are additive
        # for that reason — they explain the shape without changing it.
        return json.dumps({
            "error": f"Job '{job_id}' not found.",
            "job_store_started": jobs.STARTED_AT.isoformat(),
            "hint": ("Job ids live in this daemon's memory and do not survive a restart. "
                     "If this job was submitted before job_store_started, the daemon "
                     "restarted and the work's outcome is UNKNOWN, not failed — confirm "
                     "with vault_stats()/vault_inbox_status() before re-running, since "
                     "the job may well have completed."),
        })
    if job["status"] == "running":
        from datetime import datetime as dt
        elapsed = (dt.now() - dt.fromisoformat(job["started"])).total_seconds()
        job["elapsed_seconds"] = int(elapsed)
        hint = jobs.duration_hint(job["task"])
        if hint:
            job["typical_seconds"] = hint["typical_seconds"]
            job["check_again_in_seconds"] = jobs.next_check_seconds(hint, elapsed)
            # Reported only when the history cannot support a single number.
            # A task whose runs span an order of magnitude has been two jobs
            # under one name, and answering with the median alone told a caller
            # "3.2 seconds" about a run that took 178.4 — see duration_hint.
            if hint["spread_is_wide"]:
                job["fastest_seconds"] = hint["fastest_seconds"]
                job["slowest_seconds"] = hint["slowest_seconds"]
                job["estimate_note"] = (
                    f"This task's {hint['runs_recorded']} recorded runs span "
                    f"{hint['fastest_seconds']}s to {hint['slowest_seconds']}s, so the median "
                    f"predicts little. Wait check_again_in_seconds rather than polling."
                )
    return json.dumps(job)


# ── external ingestion ────────────────────────────────────────────────────────

@mcp.tool()
async def ingest_folder(source_path: str, recursive: bool = True, force: bool = False,
                        exclude: list[str] | None = None) -> str:
    """
    Index all supported files from an external folder into vault search.
    Original files are NEVER moved or modified. Results are tagged folder='_external'.
    Re-running is safe, files are upserted, not duplicated.
    For large directories, prefer ingest_folder_bg().

    force: re-index even if file mtime and size are unchanged (default False).
    exclude: glob patterns to leave out, matched against the path relative to
    source_path, the file name, or any single path component. So `Logs/*`,
    `*.log` and `Logs` all work, and all three are common. Use it for the
    subdirectory that holds build output, manifests or logs: those are text, so
    the extractor takes them, and they cost embedding time while answering
    nothing. The count comes back as `excluded`.
    """
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, lambda: _ingest.ingest(source_path, recursive=recursive, force=force, exclude=exclude))
    return json.dumps(result)


@mcp.tool()
async def ingest_folder_bg(source_path: str, recursive: bool = True, force: bool = False,
                           exclude: list[str] | None = None) -> str:
    """Index files from an external folder in the background. Returns a job_id immediately.

    force and exclude take the same values as ingest_folder().
    """
    job_id = jobs.submit("ingest_folder", _ingest.ingest, source_path, recursive, force, exclude)
    return json.dumps({"job_id": job_id, "source": source_path, "status": "running",
                       "message": "Ingestion started. Call task_status(job_id) to check progress."})


@mcp.tool()
async def ingest_configured(name: str = "", force: bool = False) -> str:
    """Ingest a configured source by name, or every enabled source when name is empty.

    Configure `ingest_sources` and `ingest_exclude_patterns` in config.json first.
    This is the declarative route: the configured path, recursion setting and
    source-specific exclusions are used as recorded, rather than supplied again.
    """
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, lambda: _ingest.ingest_configured(name, force))
    return json.dumps(result)


@mcp.tool()
async def ingest_configured_bg(name: str = "", force: bool = False) -> str:
    """Start configured ingestion in the background; omit name to run all enabled sources."""
    job_id = jobs.submit("ingest_configured", _ingest.ingest_configured, name, force)
    return json.dumps({"job_id": job_id, "source": name or "all configured sources", "status": "running",
                       "message": "Configured ingestion started. Call task_status(job_id) to check progress."})


@mcp.tool()
async def ingest_status() -> str:
    """Return the ingestion registry: which external paths have been indexed and when."""
    return json.dumps(_ingest.status())


@mcp.tool()
async def ingest_forget(source_path: str) -> str:
    """
    Drop everything previously indexed from an external path (the inverse of ingest_folder).
    Use when an ingested folder was moved, deleted, or should no longer answer searches —
    otherwise its rows keep surfacing with paths that no longer resolve.
    Original files are never touched.
    """
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, lambda: _ingest.forget(source_path))
    return json.dumps(result)


# ── code graph ─────────────────────────────────────────────────────────────────

@mcp.tool()
async def graph_preview(path: str, name: str = "", exclude: list[str] | None = None) -> str:
    """
    CALL THIS BEFORE graph_build on an unfamiliar directory.
    Reports what a build would cover — file counts by type, code files by extension,
    the vault folder it would write into, and whether a graph of that name already
    exists — without building or writing anything. Takes seconds.
    Also flags JS bundles whose .js.map can be reconstructed into real sources
    (see extract_source_maps); graphing a bundle directly yields one useless module.
    'scale_reference' lists graphs already built on this machine so article counts
    can be judged against real measurements.
    """
    try:
        result = await asyncio.to_thread(graphbridge.preview_graph, _vault.cfg, path, name or None, exclude)
    except ModuleNotFoundError as e:
        return json.dumps({"error": f"code-graph pipeline not installed: {e}. "
                                    'Run: pip install "delegation-core[graph]"'})
    return json.dumps(result)


@mcp.tool()
async def extract_source_maps(source_path: str, out_dir: str) -> str:
    """
    Reconstruct original sources from .js.map sidecars into out_dir, then graph THAT.
    npm-installed tools ship a single bundle whose map usually carries every original
    file verbatim; the reconstructed tree graphs into real package structure while the
    bundle would collapse into one node. out_dir must be empty or nonexistent.
    """
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, lambda: graphbridge.extract_source_maps(source_path, out_dir))
    return json.dumps(result)


@mcp.tool()
async def capabilities() -> str:
    """CALL THIS FIRST on connecting — what this server can actually do.

    Returns the live tool list (asked of the running server, so it cannot drift
    from what is actually served), the graph exporters and which tool reaches
    each, the ones deliberately left unexposed and why, capabilities that exist
    in the code but are still unwired, and the search scopes.

    Prefer this over any prose description of this server, AGENT_GUIDE.md
    included: prose has no guard against drifting from the code, and this
    report is generated plus test-enforced.
    """
    tools = await mcp.list_tools()
    listed = [{"name": t.name,
               "summary": (t.description or "").strip().split("\n")[0]}
              for t in sorted(tools, key=lambda t: t.name)]
    # O escopo padrao vai CALCULADO e nao escrito: veja capabilities.describe.
    try:
        health = _vault.get_health_summary()
        total = health.get("total_notes") or 0
        gerados = health.get("generated_notes") or 0
        configurado = (getattr(_vault.cfg, "default_search_scope", "") or "").strip()
        escopo = {
            "resolved": _default_scope(),
            "source": "config" if configurado else "vault-composition",
            "detail": (f"default_search_scope={configurado!r} in config.json"
                       if configurado else
                       f"{gerados} generated of {total} notes"),
        }
    except Exception as e:
        escopo = {"resolved": "notes", "source": "fallback", "detail": str(e)}
    return json.dumps(_capabilities.describe(listed, escopo))


@mcp.tool()
async def graph_export(name: str, format: str) -> str:
    """Re-export an already-built graph into another format.

    format: "graphml" (Gephi, yEd, Cytoscape), "svg" (static image, embeds
    anywhere), or "cypher" (replay script for Neo4j). Reads the existing
    graph.json, so it costs seconds — no re-extraction. Run graph_build first.
    """
    result = await asyncio.to_thread(graphbridge.export_graph, _vault.cfg, name, format)
    return json.dumps(result)


@mcp.tool()
async def graph_build_bg(path: str, name: str = "", force: bool = False,
                         exclude: list[str] | None = None) -> str:
    """
    Build a code knowledge graph in the background. Returns a job_id immediately.
    Prefer this over graph_build for anything sizeable: a real codebase takes minutes,
    which exceeds most MCP client timeouts.
    exclude: gitignore-syntax patterns to leave out (see graph_build).
    """
    def _run():
        return asyncio.run(
            graphbridge.build_graph(_vault.cfg, _vault, path, name=name or None,
                                    force=force, exclude=exclude))

    job_id = jobs.submit("graph_build", _run)
    return json.dumps({"job_id": job_id, "path": path, "status": "running",
                       "message": "Graph build started. Call task_status(job_id) to check progress."})


@mcp.tool()
async def graph_build(path: str, name: str = "", force: bool = False,
                      exclude: list[str] | None = None) -> str:
    """
    Build a code knowledge graph for a local directory: detect -> AST extract
    (tree-sitter, code files only) -> build -> cluster -> analyze -> report -> export.
    Writes graph.json/graph.html/callflow.html/GRAPH_REPORT.md/wiki articles under
    ~/.delegation_core/graphs/<name>/ and files the report + wiki articles directly
    into the vault (no LLM involved — they're already well-formed), so they become
    searchable through search_vault.
    name defaults to the directory's basename. Re-running the same name is a no-op
    unless force=true (rebuilds and overwrites).
    exclude: gitignore-syntax patterns to leave out of the scan, e.g.
    ["tests/", "website/", "vendor/"]. Worth setting on any large repository —
    one real build spent 40% of its 2704 wiki articles on communities made
    entirely of test files, which then had to be pruned out of the vault by
    hand. Pass the same patterns to graph_preview() to size the build first.
    Blocks until done — call graph_preview() first to see the scale, and prefer
    graph_build_bg() for anything that might outlast the client's timeout.
    Requires the [graph] extra: pip install "delegation-core[graph]".
    """
    try:
        # graphbridge.build_graph is `async def` in name only — detect/extract/
        # build/cluster/analyze/report/export/wiki all run synchronously inside
        # it (see its own docstring: "nothing inside actually awaits"), so a
        # plain `await` here would run the entire tree-sitter + clustering +
        # report pipeline directly on the event loop — for a real codebase that
        # can be seconds to minutes, stalling any other tool call this same
        # client fires concurrently (FastMCP runs each incoming request as its
        # own task on one loop). asyncio.run() in a worker thread keeps this
        # tool's own "block until built" contract while freeing the loop.
        result = await asyncio.to_thread(
            asyncio.run,
            graphbridge.build_graph(_vault.cfg, _vault, path, name=name or None, force=force),
        )
    except ModuleNotFoundError as e:
        return json.dumps({"error": f"code-graph pipeline not installed: {e}. "
                                    'Run: pip install "delegation-core[graph]"'})
    return json.dumps(result)


@mcp.tool()
def graph_list(name: str = "") -> str:
    """List previously built code graphs: name, source path, node/edge/community counts, last built.

    Vault note paths are returned as a count (`vault_notes_filed`). Pass `name` to
    get that one graph's entry with the full `vault_paths` list.
    """
    return json.dumps(graphbridge.list_graphs(_vault.cfg, name=name or None))


# A GRAPH_REPORT.md is a whole markdown document, and on this machine the two
# largest are 335,350 and 319,254 characters — both past the MCP tool-result cap,
# so graph_report returned nothing readable for precisely the graphs big enough
# to be worth a report. Paged rather than summarised: unlike graph_list's
# vault_paths, every line here is content the caller explicitly asked for.
# Only the MCP path pages. graphbridge.get_report() still returns the document
# whole for the dashboard and the CLI, which render it locally and have no cap.
GRAPH_REPORT_PAGE_CHARS = 30_000


def _page_report(result: dict, offset: int, page_chars: int = GRAPH_REPORT_PAGE_CHARS) -> dict:
    """Cut one page out of a get_report() result, leaving error results alone."""
    report = result.get("report")
    if report is None:
        return result
    total = len(report)
    start = min(max(offset, 0), total)
    page = report[start:start + page_chars]
    result["report"] = page
    result["offset"] = start
    result["total_chars"] = total
    if start + len(page) < total:
        result["next_offset"] = start + len(page)
        result["truncated"] = True
    return result


@mcp.tool()
def graph_report(name: str, offset: int = 0) -> str:
    """Return the GRAPH_REPORT.md for a previously built graph by name (see graph_list).

    Long reports are paged. When `truncated` is true, call again with
    offset=`next_offset` to read on; `total_chars` is the report's full length.
    """
    return json.dumps(_page_report(graphbridge.get_report(_vault.cfg, name), offset))


@mcp.tool()
async def graph_affected(name: str, query: str, depth: int = 2) -> str:
    """
    Blast-radius query: what else is affected if `query` (a file path or symbol
    label, e.g. "auth.py" or "AuthService.login") changes? Walks calls/
    indirect_call/references/imports edges backward up to `depth` hops.
    Requires graph_build(name=...) to have run first.
    """
    try:
        # get_affected is a plain sync function (graph.json load + networkx
        # rebuild + BFS) called with no offload — smaller than graph_build's
        # full pipeline but the same shape of problem for a large graph. Offload
        # for consistency with every other sync-heavy call site in this file
        # (relink_folder, ingest_folder, search_web already do this).
        result = await asyncio.to_thread(graphbridge.get_affected, _vault.cfg, name, query, depth=depth)
    except ModuleNotFoundError as e:
        return json.dumps({"error": f"code-graph pipeline not installed: {e}. "
                                    'Run: pip install "delegation-core[graph]"'})
    return json.dumps(result)


@mcp.tool()
async def graph_hook_install(path: str, name: str = "") -> str:
    """
    Install a git post-commit hook that rebuilds this repo's code graph
    automatically after every commit (background, code-only, no LLM, no vault
    writes — call graph_build explicitly whenever you want the latest report
    filed into the vault). path must be inside a git repository.
    """
    return json.dumps(graph_hook.install(path, name=name or None))


@mcp.tool()
async def graph_hook_uninstall(path: str) -> str:
    """Remove the graph auto-rebuild post-commit hook installed by graph_hook_install."""
    return json.dumps(graph_hook.uninstall(path))


@mcp.tool()
async def graph_hook_status(path: str) -> str:
    """Check whether the graph auto-rebuild post-commit hook is installed for this repo."""
    return json.dumps(graph_hook.status(path))


# ── process tracking ──────────────────────────────────────────────────────────

@mcp.tool()
@_serialized
def process_create(name: str, description: str = "", steps: str = "") -> str:
    """
    Track a new ongoing process that persists across sessions and server restarts.
    Use whenever a task spans multiple conversations or requires follow-up.
    steps: comma-separated list of steps (optional).
    """
    step_list = [s.strip() for s in steps.split(",") if s.strip()] if steps else []
    return json.dumps(_tracker.create(name=name, description=description, steps=step_list))


@mcp.tool()
def process_list(status: str = "active", query: str = "") -> str:
    """List tracked processes. status: active|paused|done|cancelled|all."""
    processes = _tracker.list_processes(status=status, query=query)
    return json.dumps({
        "status_filter": status, "query": query, "count": len(processes),
        "processes": [
            {
                "id":          p["id"],
                "name":        p["name"],
                "status":      p["status"],
                "description": p["description"],
                "steps_done":  f"{sum(s['done'] for s in p['steps'])}/{len(p['steps'])}" if p["steps"] else "open",
                "last_note":   p["notes"][-1]["text"] if p["notes"] else "",
                "updated":     p["updated"],
            }
            for p in processes
        ],
    })


@mcp.tool()
@_serialized
def process_update(process_id: str, note: str = "", step_done: int = -1, status: str = "") -> str:
    """Update a tracked process. All parameters optional — only set what changed."""
    _VALID_STATUSES = {"", "active", "paused", "done", "cancelled"}
    if status not in _VALID_STATUSES:
        return json.dumps({"error": f"Invalid status '{status}'. Valid: active, paused, done, cancelled"})
    proc = _tracker.update(process_id=process_id, note=note, step_done=step_done, status=status)
    if proc is None:
        return json.dumps({"error": f"Process not found: {process_id}"})
    return json.dumps(proc)


@mcp.tool()
def process_get(process_id: str) -> str:
    """Get full details of a tracked process including all steps, notes, and history."""
    proc = _tracker.get(process_id)
    if proc is None:
        return json.dumps({"error": f"Process not found: {process_id}"})
    return json.dumps(proc)


# ── entry point ───────────────────────────────────────────────────────────────

def run_server(cfg: Config):
    global _engine, _vault, _tracker, _ingest

    if not cfg.vault.exists():
        sys.stderr.write(f"FATAL: Vault not found at {cfg.vault}\n")
        sys.stderr.write("Run 'delegation-core setup' to configure.\n")
        sys.exit(1)

    _engine  = DelegationEngine(cfg)
    _vault   = VaultManager(cfg)
    _tracker = ProcessTracker(cfg.processes_path)
    _ingest  = IngestManager(_vault)

    def _cleanup():
        # Runs at interpreter shutdown via atexit — a *synchronous* context.
        # v5.1 patch: the previous asyncio.get_event_loop() is deprecated in
        # Python 3.12 when there is no current loop (and will raise in a future
        # release), which made this handler silently no-op on the Deprecation
        # warning path. atexit never has a running loop, so we simply spin up a
        # fresh one with asyncio.run() to flush _engine.aclose() (closes the
        # httpx.AsyncClient to llama.cpp). get_running_loop() is checked first
        # only to defend against the theoretical case of atexit firing from
        # inside a live loop, where a fresh asyncio.run() would raise.
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop and loop.is_running():
                loop.create_task(_engine.aclose())
            else:
                asyncio.run(_engine.aclose())
        except Exception:
            pass
        _cleanup_own_session_file()
    atexit.register(_cleanup)

    mcp.add_middleware(_ClientTrackingMiddleware())

    # Wired here rather than at module import so that importing server.py — which
    # the tests and the capability registry both do — never has the side effect of
    # generating and persisting a secret.
    mcp.auth = LocalTokenAuth(
        cfg.ensure_server_token(),
        base_url=f"http://{cfg.server_host}:{cfg.server_port}",
    )

    # Non-blocking. BGE + ChromaDB load on a background thread while the transport
    # comes up; every public VaultManager method already guards with
    # _ensure_ready(), so a tool call that lands mid-load waits for it instead of
    # failing, and _init() deliberately leaves _initialized False on error so a
    # transient GPU OOM retries on the next call rather than wedging the daemon.
    #
    # This was a blocking _vault._init() followed by a hard exit when the
    # collection came back empty. Under stdio the cost was paid once per client
    # spawn and nothing observed it. Over HTTP the client times the handshake —
    # Codex 0.147.0 defaults to startup_timeout_sec = 10 — and a machine that has
    # to download BGE-m3 first blows through that by minutes. Measured on this
    # machine with the model already cached: 3.94s from "Loading BGE model" to
    # "ready", which fits, but nothing about that margin is guaranteed elsewhere.
    #
    # Dropping the hard exit is deliberate too: for a daemon that outlives every
    # client, dying on a transient GPU OOM is strictly worse than serving and
    # retrying, which is the behaviour _ensure_ready() was built for.
    # Armed before anything that can hang, so a startup that deadlocks before
    # the loop exists is covered too. The lifespan takes over re-arming.
    _arm_watchdog(getattr(cfg, "loop_watchdog_sec", 0))

    _vault.warm_up()
    _retomar_reconstrucao_na_partida()

    # The dashboard's JSON API, on the daemon's own VaultManager. It was a
    # sidecar the Tauri app spawned, which stopped making sense the moment this
    # server got a network transport: the sidecar existed because mcp.run()
    # serves one transport at a time, so a dashboard could not attach to the MCP
    # server and needed a process of its own. That process paid for the
    # separation in a second resident BGE-m3 (2314 MiB, measured on this
    # machine) and a second ChromaDB opener on one index — the same duplication
    # the daemon exists to remove.
    #
    # A bind failure is logged, not fatal: the dashboard is an accessory, and a
    # busy port (usually a second daemon, or a sidecar still running from the
    # old model) is not a reason to take MCP service down with it.
    # The task line's single consumer. Started here rather than lazily on first
    # submit so that tasks scheduled by a previous run — and tasks left running
    # when that run died — are picked up at boot rather than whenever someone
    # next happens to call a tool.
    _worker = LocalTaskWorker(_engine)
    _worker.start()
    atexit.register(_worker.stop)

    if cfg.dashboard_port:
        try:
            # Imported here, not at module scope: dashboard_api pulls in the
            # http.server stack and is irrelevant to every other importer of
            # this module (the tests and the capability registry both import it).
            from . import dashboard_api

            dashboard_api.serve_in_process(
                cfg, _vault, _tracker,
                host=cfg.server_host, port=cfg.dashboard_port,
            )
        except OSError as e:
            logger.warning(
                "dashboard API not served on port %d (%s) — the dashboard will "
                "fall back to spawning its own sidecar", cfg.dashboard_port, e,
            )

    from . import __version__ as _version
    logger.info(
        "delegation-core v%s ready on %s — vault: %s | llama: %s | budget: %s "
        "| synthesis: %s (%s) | split: %d chars / %d notes max",
        _version, cfg.server_url, cfg.vault, cfg.llama_url, cfg.budget_mode,
        "on" if cfg.synthesis_enabled else "off", cfg.synthesis_lang,
        cfg.split_min_chars, cfg.split_max_notes,
    )
    # The tool count used to be hardcoded into that line and read 31 while the
    # server served 49 — the drift this module's own docstring warns about.
    # capabilities() reports the live list; a log line should not compete with it.
    mcp.run(
        transport="http",
        host=cfg.server_host,
        port=cfg.server_port,
        path=cfg.server_path,
    )
