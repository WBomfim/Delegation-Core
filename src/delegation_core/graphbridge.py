"""
graphbridge.py — Orchestrates the vendored code-graph pipeline (delegation_core.graph,
adapted from Graphify: github.com/Graphify-Labs/graphify — see that package's
docstring for exactly what was and wasn't vendored) end-to-end, and files its
output into the vault directly instead of Graphify's own Obsidian writer
(to_obsidian()).

AST-only: only the code-file extraction path is used. Graphify's semantic
(doc/paper/image) extraction pass requires an LLM backend, which was deliberately
not vendored — see delegation_core/graph/__init__.py for the include/exclude list.

New in v0.7.0. v0.7.1 added callflow.html (Mermaid architecture diagrams),
wiki/ articles (one vault note per community/god-node instead of one big
report), and graph_affected (blast-radius query).

v0.7.2 correction: the wiki/report artifacts are now written to the vault
DIRECTLY (write + index_note), not routed through _inbox/ + organizer.run().
The original design queued every wiki article (often 100+) through the FULL
classify/synthesize pipeline meant for messy raw documents — since each
synthesize() call is a real LLM round-trip, one graph_build() turned into 100+
sequential llama.cpp calls (tens of minutes, observed directly). Wiki articles
and GRAPH_REPORT.md are already clean, structured markdown
(report.generate()/wiki.py's own templates) — they don't need extraction or
rewriting, only filing and indexing. This also means graph_build no longer
needs a DelegationEngine at all.

Filing layout (corrected after the first real multi-hundred-article build, a
598-article graph of Graphify itself):

    <Reference>/<date>-Code Graph Report — <graph>.md   report, with BGE backlinks
    <Reference>/graphs/<graph>/index.md                 wiki nav root
    <Reference>/graphs/<graph>/Community_0.md, …        articles, original stems

Articles keep their wiki.py stems (so the relative links between them resolve)
and get no BGE "## Related" block (they already carry exact graph-derived
cross-links, and computing semantic ones cost a search + rewrite + backlink pass
each — the bulk of a build's wall time). Every filed path is recorded in the
registry so the next rebuild can clear its predecessor.
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("graphbridge")


def _load_registry(cfg) -> dict:
    path = cfg.graphs_registry_path
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception as e:
        logger.warning("Could not load graphs registry: %s", e)
        return {}


def _save_registry(cfg, registry: dict) -> None:
    cfg.graphs_dir.mkdir(parents=True, exist_ok=True)
    cfg.graphs_registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")


def _slugify(name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-")
    return safe or "graph"


def _resolve_folder(cfg, preferred: str = "reference") -> str:
    """Case-insensitive match against cfg.vault_folders (vaults configure their
    own casing — e.g. "Reference" vs the config.py default "reference" — and
    writing to the wrong case silently creates a stray duplicate folder instead
    of erroring, so this has to be resolved explicitly rather than assumed).
    Falls back to the first configured folder if nothing matches "reference".
    """
    for f in cfg.vault_folders:
        if f.lower() == preferred:
            return f
    return cfg.vault_folders[0] if cfg.vault_folders else preferred


#: Subfolder under the resolved vault folder that holds every graph's wiki.
#: One directory per graph name, keeping generated articles out of the folder
#: root — a single mid-sized repo produces hundreds of them (graphify: 598),
#: which buries hand-written notes in Obsidian's file tree and in vault_list_notes.
WIKI_SUBDIR = "graphs"


def _frontmatter(title: str, extra: str = "") -> str:
    from .vault import yaml_quote_scalar
    return (
        f"---\ntitle: {yaml_quote_scalar(title)}\ndate: {datetime.now().strftime('%Y-%m-%d')}\n"
        f"ai_generated: false\nsource: graph_build\n{extra}---\n\n"
    )


def _write_vault_note(vault_manager, folder: str, title: str, content: str) -> str:
    """Direct write: file + index_note + best-effort BGE-only wikilinks. No LLM
    call — for content that's already well-formed (a generated report/article),
    not raw material that needs extraction or rewriting. Mirrors write_note()'s
    shape in server.py, minus the "ai_generated: true" framing since nothing
    here was LLM-authored, just LLM-analyzed.
    """
    from .linker import inject_backlinks, wikilinks
    from .vault import safe_filename

    cfg = vault_manager.cfg
    safe = safe_filename(title)
    dest = cfg.vault / folder / f"{datetime.now().strftime('%Y-%m-%d')}-{safe}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    full = _frontmatter(title) + content
    dest.write_text(full, encoding="utf-8")
    rel = dest.relative_to(cfg.vault).as_posix()
    vault_manager.index_note(full, vault_manager.note_metadata(rel, title, folder))

    try:
        hits = [h for h in vault_manager.search(full[:600], limit=6) if h.get("path") != rel][:5]
        links = wikilinks(hits, cfg.merge_threshold, cfg.vault)
        if links:
            updated = full.rstrip() + f"\n\n## Related\n{links}\n"
            dest.write_text(updated, encoding="utf-8")
            vault_manager.index_note(updated, vault_manager.note_metadata(rel, title, folder))
            inject_backlinks(vault_manager, dest.stem,
                              [h["path"] for h in hits if h.get("similarity", 0) >= cfg.merge_threshold])
    except Exception as e:
        logger.warning("wikilink injection skipped for %s: %s", rel, e)

    return rel


def _write_wiki_article(vault_manager, wiki_folder: str, graph_name: str, article: Path) -> str:
    """File one wiki article under <folder>/graphs/<graph>/, keeping its original stem.

    Two deliberate differences from _write_vault_note:

    * **The filename is the wiki slug, not a dated safe_filename.** wiki.py emits
      relative links between its own articles (``[Community 25](Community_25.md)``).
      Renaming to ``2026-07-31-graphify_ Community_25.md`` broke every one of those
      links; preserving the stem inside a per-graph directory makes the wiki
      self-consistent in Obsidian for free, with no link rewriting.
    * **No BGE "## Related" block.** These articles already carry dense, exact
      cross-links computed from the graph itself. Semantic backlinks on top added
      ~65 near-identical entries per article (every community resembles every other
      community) and cost one search + one rewrite + one backlink pass per article
      — the dominant share of a build's wall time at this article count.
    """
    content = article.read_text(encoding="utf-8")
    title = f"{graph_name}: {article.stem}"
    cfg = vault_manager.cfg
    dest = cfg.vault / wiki_folder / f"{article.name}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    full = _frontmatter(title, extra=f"graph: {graph_name}\n") + content
    dest.write_text(full, encoding="utf-8")
    rel = dest.relative_to(cfg.vault).as_posix()
    vault_manager.index_note(full, vault_manager.note_metadata(rel, title, wiki_folder))
    return rel


def _clear_previous_filing(vault_manager, previous_paths: list[str], wiki_dir_rel: str) -> int:
    """Remove a graph's previously filed vault notes before refiling it.

    Without this, every rebuild left the last run's articles behind: community
    numbering is not stable across runs, and the dated filenames meant a rebuild
    on a later day never overwrote anything. Deletes the ChromaDB rows too, so
    stale articles stop surfacing in search immediately rather than at the next
    full reindex.
    """
    from .notes import resolve_in_vault

    cfg = vault_manager.cfg
    stale: list[str] = []

    # Contencao no unico caminho do projeto que APAGA nota do vault.
    #
    # `resolve_in_vault` existe e o docstring dela registra que esta mesma falha
    # foi corrigida duas vezes antes, em relink_folder e na rota de nota do
    # dashboard, e que por isso a checagem mora num lugar so "rather than being
    # re-typed at each new call site". Aqui ela nao era chamada, e a unica
    # checagem era `is_file()`, que responde True para `../fora_do_vault.md`.
    #
    # Nao e explorável hoje: `graph_name` passa por `_slugify`, que derruba tudo
    # que nao seja [a-zA-Z0-9_-], entao "../../Sessions" chega aqui como
    # "Sessions". Mas essa defesa e INCIDENTAL -- o slug existe para gerar nome
    # de diretorio, nao para conter travessia -- e aceitar um ponto no nome, para
    # um grafo "v1.2", a desfaz sem que nada ligue uma coisa a outra. Medido com
    # a contencao removida: um wiki_dir_rel de "Reference/graphs/../../Sessions"
    # fez o rglob varrer Sessions/ e o unlink apagar a nota do usuario.
    # Conter no VAULT nao basta, e a primeira versao disto so continha no vault:
    # "Reference/graphs/../../Sessions" resolve para <vault>/Sessions, que esta
    # dentro do vault e e a pasta de notas do usuario. Medido com essa versao no
    # lugar: o rglob varreu Sessions/ e a nota do usuario foi apagada assim
    # mesmo. O que precisa ser verdade e mais estreito: a pasta limpa aqui tem
    # que ser um filho direto de <vault>/<pasta>/graphs.
    wiki_root = resolve_in_vault(cfg.vault, wiki_dir_rel)
    if wiki_root is None or wiki_root.parent.name != WIKI_SUBDIR:
        logger.warning("graph wiki folder %r is not a %s/<name> directory inside "
                       "the vault - refusing to clear it", wiki_dir_rel, WIKI_SUBDIR)
    elif wiki_root.is_dir():
        stale += [p.relative_to(cfg.vault).as_posix() for p in wiki_root.rglob("*.md")]
    for rel in previous_paths or []:
        p = resolve_in_vault(cfg.vault, rel)
        if p is None:
            logger.warning("stale graph note %r resolves outside the vault - "
                           "refusing to remove it", rel)
            continue
        if p.is_file() and rel not in stale:
            stale.append(rel)

    # A contencao mora na MONTAGEM de `stale`, acima, e nao aqui: as duas
    # entradas passam por resolve_in_vault antes de entrar na lista. Uma segunda
    # checagem neste laco sobreviveu a mutacao -- nenhum teste conseguia
    # distingui-la -- e uma guarda que nenhum teste alcanca e peso morto que
    # sugere protecao onde nao ha nenhuma nova. Um lugar so, e ele e testado.
    for rel in stale:
        try:
            (cfg.vault / rel).unlink()
        except OSError as e:
            logger.warning("Could not remove stale graph note %s: %s", rel, e)
    vault_manager.delete_notes(stale)
    return len(stale)


def _write_artifacts_to_vault(vault_manager, graph_name: str, report_md: str,
                               wiki_dir: Path | None, previous_paths: list[str] | None = None) -> dict:
    """File GRAPH_REPORT.md + the per-community/god-node wiki articles into the vault.

    Layout:
        <Reference>/<date>-Code Graph Report — <graph>.md   ← discoverable entry point
        <Reference>/graphs/<graph>/index.md                 ← wiki nav root
        <Reference>/graphs/<graph>/Community_0.md, ...      ← articles, original stems

    The report stays in the folder root because it is the one note a human or a
    search is likely to want; the wiki is a self-contained directory beside it.
    """
    folder = _resolve_folder(vault_manager.cfg, "reference")
    wiki_folder = f"{folder}/{WIKI_SUBDIR}/{graph_name}"

    removed = _clear_previous_filing(vault_manager, previous_paths or [], wiki_folder)

    written = [_write_vault_note(vault_manager, folder, f"Code Graph Report — {graph_name}", report_md)]

    wiki_written: list[str] = []
    if wiki_dir is not None and wiki_dir.is_dir():
        for article in sorted(wiki_dir.glob("*.md")):
            wiki_written.append(_write_wiki_article(vault_manager, wiki_folder, graph_name, article))

    todos = written + wiki_written
    # Sem isto, cada uma destas notas fica sem carimbo em .chroma_index.json e o
    # proximo reindex incremental a reembute inteira, mesmo intocada. Num grafo
    # de tamanho real sao milhares: e o que transformava um reindex de cinco
    # notas novas em uma reconstrucao de vinte e cinco minutos.
    vault_manager.stamp_indexed(todos)

    return {
        "report_path": written[0],
        "wiki_folder": wiki_folder,
        "wiki_count": len(wiki_written),
        "replaced_stale": removed,
        "written_paths": todos,
    }


def find_source_maps(root: Path, limit: int = 20) -> list[dict]:
    """Find JS bundles shipping their original sources in a .map sidecar.

    An npm-installed tool is usually a single esbuild/rollup bundle — graphing it
    yields one enormous meaningless module. But the .js.map beside it commonly
    carries `sourcesContent`: every original source file, verbatim. paperclipai's
    3.3 MB map held all 303 of its TypeScript files, none of which existed on
    disk in any other form. Reconstructing them turns an opaque bundle into a
    real graph (3682 nodes across the actual package structure).

    Cheap by construction: reads only the map's head to test for the key, and
    parses in full only then.
    """
    # Skip judged on the path *relative to root*, not the absolute path: pointing
    # at a project must not surface every dependency's map (voicebox reported
    # lucide-react's 1417 sources), while pointing directly at a globally
    # installed package — which necessarily lives under node_modules — still works.
    # Deliberately NOT the graph pipeline's _SKIP_DIRS: dist/, build/ and out/ are
    # exactly where a bundle and its map live, so skipping them would defeat the
    # whole check. Only dependency and VCS trees are excluded.
    skip = {"node_modules", ".git", "__pycache__", "vendor",
            ".next", ".nuxt", ".cache", "coverage", ".yarn", "bower_components"}

    found: list[dict] = []
    for m in sorted(root.rglob("*.js.map")):
        if len(found) >= limit:
            break
        if skip & set(m.relative_to(root).parts[:-1]):
            continue
        try:
            if '"sourcesContent"' not in m.read_text(encoding="utf-8", errors="ignore")[:4096]:
                # The key can sit past the head on some emitters; size-gate the
                # full parse so this stays cheap on large trees.
                if m.stat().st_size > 40_000_000:
                    continue
            data = json.loads(m.read_text(encoding="utf-8"))
        except Exception:
            continue
        contents = data.get("sourcesContent") or []
        usable = sum(1 for c in contents if c)
        if not usable:
            continue
        exts: dict[str, int] = {}
        for s in data.get("sources") or []:
            ext = Path(s).suffix.lower() or "(none)"
            exts[ext] = exts.get(ext, 0) + 1
        found.append({
            "map": str(m),
            "bundle": str(m.with_suffix("")) if m.with_suffix("").exists() else None,
            "reconstructable_sources": usable,
            "by_extension": dict(sorted(exts.items(), key=lambda kv: -kv[1])[:6]),
        })
    return found


def extract_source_maps(source_path: str, out_dir: str, limit: int = 20) -> dict:
    """Reconstruct original sources from every usable .js.map under source_path.

    Bundler-relative paths ("../../packages/shared/src/x.ts") are normalised into
    a clean tree so the reconstructed layout matches the real package structure,
    which is what makes the resulting graph meaningful.
    """
    root = Path(source_path).expanduser().resolve()
    out = Path(out_dir).expanduser().resolve()
    if not root.exists():
        return {"error": f"Path not found: {source_path}"}
    if out.exists() and any(out.iterdir()):
        return {"error": f"Output directory is not empty: {out}"}

    maps = find_source_maps(root, limit=limit)
    if not maps:
        return {"status": "empty", "message": f"No .js.map with sourcesContent under {root}."}

    written = 0
    roots: dict[str, int] = {}
    for entry in maps:
        try:
            data = json.loads(Path(entry["map"]).read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Could not parse %s: %s", entry["map"], e)
            continue
        for src, content in zip(data.get("sources") or [], data.get("sourcesContent") or []):
            if not content:
                continue
            parts = [p for p in Path(src).parts if p not in ("..", ".")]
            if not parts:
                continue
            rel = Path(*parts)
            roots[rel.parts[0]] = roots.get(rel.parts[0], 0) + 1
            dest = out / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
            written += 1

    return {"status": "ok", "source_path": str(root), "out_dir": str(out),
            "maps_used": len(maps), "files_written": written,
            "top_level": dict(sorted(roots.items(), key=lambda kv: -kv[1]))}


def preview_graph(cfg, source_path: str, name: str | None = None,
                  exclude: list[str] | None = None) -> dict:
    """Report what a graph_build would cover, without building anything.

    Added because the first real build filed 598 wiki articles into the vault
    before anyone could see it coming, and undoing that took a migration script.
    detect() already computes the file inventory in seconds — surfacing it is the
    difference between an informed build and a surprise.

    No community estimate is invented here: instead, previously built graphs on
    this machine are returned as `scale_reference`, so the caller can interpolate
    from real measurements rather than a made-up formula.
    """
    from delegation_core.graph.detect import detect

    root = Path(source_path).expanduser().resolve()
    if not root.exists():
        return {"error": f"Path not found: {source_path}"}
    if not root.is_dir():
        return {"error": f"Not a directory: {source_path}"}

    graph_name = _slugify(name or root.name)
    try:
        detection = detect(root, cache_root=cfg.graphs_dir / graph_name / "cache",
                           extra_excludes=exclude)
    except Exception as e:
        return {"error": f"detect() failed: {e}"}

    files = detection.get("files", {}) or {}
    code = [Path(p) for p in files.get("code", [])]
    exts: dict[str, int] = {}
    for p in code:
        ext = p.suffix.lower() or "(none)"
        exts[ext] = exts.get(ext, 0) + 1

    registry = _load_registry(cfg)
    existing = registry.get(graph_name)
    scale = [
        {"name": n, "node_count": e.get("node_count"), "edge_count": e.get("edge_count"),
         "community_count": e.get("community_count"),
         "vault_notes_filed": len(e.get("vault_paths") or [])}
        for n, e in registry.items() if e.get("node_count")
    ]

    folder = _resolve_folder(cfg, "reference")
    result = {
        "status": "ok",
        "name": graph_name,
        "source_path": str(root),
        "counts": {k: len(v) for k, v in files.items()},
        "code_by_extension": dict(sorted(exts.items(), key=lambda kv: -kv[1])[:12]),
        "total_files": detection.get("total_files"),
        "total_words": detection.get("total_words"),
        "would_write_to": f"{folder}/{WIKI_SUBDIR}/{graph_name}",
        "already_built": bool(existing),
        "scale_reference": scale,
    }
    if existing:
        result["previous_build"] = {
            "built_at": existing.get("built_at"),
            "node_count": existing.get("node_count"),
            "community_count": existing.get("community_count"),
            "vault_notes_filed": len(existing.get("vault_paths") or []),
            "note": "graph_build is a no-op unless force=true; a rebuild replaces these notes.",
        }
    if not code:
        result["status"] = "empty"
        result["message"] = f"No recognized code files under {root}."

    maps = find_source_maps(root, limit=5)
    if maps:
        total = sum(m["reconstructable_sources"] for m in maps)
        result["source_maps"] = maps
        result["source_map_hint"] = (
            f"{total} original source file(s) are reconstructable from .js.map sidecars. "
            "Graphing a bundle directly yields one meaningless module — run "
            "extract_source_maps() first and graph the reconstructed tree instead."
        )
    return result


async def build_graph(cfg, vault_manager, source_path: str,
                       name: str | None = None, force: bool = False,
                       file_to_vault: bool = True,
                       exclude: list[str] | None = None) -> dict:
    """Build a code knowledge graph for source_path using the vendored pipeline.

    Writes graph.json / graph.html / callflow.html / GRAPH_REPORT.md / wiki/*.md
    to cfg.graphs_dir/<name>/. When file_to_vault=True (default — the MCP tool's
    behavior) also files GRAPH_REPORT.md + the wiki articles directly into the
    vault (see _write_artifacts_to_vault). file_to_vault=False is for the git
    post-commit hook path (graph_hook.py): keep the on-disk artifacts fresh on
    every commit without touching the vault each time — vault filing stays a
    deliberate action.

    async only because it's called from server.py's async MCP tools; nothing
    inside actually awaits (no engine/LLM involved — see module docstring).
    """
    from delegation_core.graph.detect import detect
    from delegation_core.graph.extract import extract as ast_extract
    from delegation_core.graph.build import build as build_graph_fn
    from delegation_core.graph.cluster import (
        cluster as cluster_fn,
        label_communities_by_hub,
        score_all,
    )
    from delegation_core.graph.analyze import god_nodes, surprising_connections, suggest_questions
    from delegation_core.graph.report import generate as render_report
    from delegation_core.graph.export import to_json, to_html
    from delegation_core.graph.callflow_html import write_callflow_html
    from delegation_core.graph.wiki import to_wiki

    root = Path(source_path).expanduser().resolve()
    if not root.exists():
        return {"error": f"Path not found: {source_path}"}
    if not root.is_dir():
        return {"error": f"Not a directory: {source_path}"}

    graph_name = _slugify(name or root.name)
    out_dir = cfg.graphs_dir / graph_name

    if out_dir.exists() and not force and (out_dir / "graph.json").exists():
        return {
            "status": "exists",
            "name": graph_name,
            "message": f"Graph '{graph_name}' already built at {out_dir}. Pass force=true to rebuild.",
            "graph_json": str(out_dir / "graph.json"),
        }

    out_dir.mkdir(parents=True, exist_ok=True)
    cache_root = out_dir / "cache"

    try:
        detection = detect(root, cache_root=cache_root, extra_excludes=exclude)
    except Exception as e:
        return {"error": f"detect() failed: {e}"}

    code_files = [Path(p) for p in detection.get("files", {}).get("code", [])]
    if not code_files:
        return {
            "status": "empty",
            "name": graph_name,
            "message": f"No recognized code files found under {root}.",
        }

    try:
        ast_result = ast_extract(code_files, cache_root=cache_root, root=root)
        G = build_graph_fn([ast_result], directed=True, root=root)
        communities = cluster_fn(G)
        # Name each community after its structural hub. Without this every
        # artifact below falls back to "Community {cid}" — a 2693-community
        # build filed 2693 vault notes titled "Community 0..2692", searchable
        # only by body text and useless as Obsidian graph node labels.
        community_labels = label_communities_by_hub(G, communities)
        cohesion = score_all(G, communities)
        gods = god_nodes(G)
        surprises = surprising_connections(G, communities)
        questions = suggest_questions(G, communities, {})

        report_md = render_report(
            G, communities, cohesion, community_labels, gods, surprises,
            detection,
            {"input_tokens": ast_result.get("input_tokens", 0),
             "output_tokens": ast_result.get("output_tokens", 0)},
            str(root), suggested_questions=questions,
        )
        (out_dir / "GRAPH_REPORT.md").write_text(report_md, encoding="utf-8")
        to_json(G, communities, str(out_dir / "graph.json"), force=True,
                community_labels=community_labels)

        try:
            to_html(G, communities, str(out_dir / "graph.html"),
                    community_labels=community_labels)
        except Exception as e:
            logger.warning("graph.html export skipped for %s: %s", graph_name, e)

        callflow_path = out_dir / "callflow.html"
        try:
            write_callflow_html(
                graph=str(out_dir / "graph.json"),
                report=str(out_dir / "GRAPH_REPORT.md"),
                output=str(callflow_path),
            )
        except Exception as e:
            logger.warning("callflow.html export skipped for %s: %s", graph_name, e)
            callflow_path = None

        wiki_dir = out_dir / "wiki"
        wiki_count = 0
        try:
            wiki_count = to_wiki(G, communities, wiki_dir,
                                 community_labels=community_labels,
                                 cohesion=cohesion, god_nodes_data=gods)
        except Exception as e:
            logger.warning("wiki export skipped for %s: %s", graph_name, e)
            wiki_dir = None
    except Exception as e:
        logger.error("Graph build failed for %s: %s", root, e)
        return {"error": f"Graph build failed: {e}"}

    registry = _load_registry(cfg)
    previous_paths = list((registry.get(graph_name) or {}).get("vault_paths") or [])
    registry[graph_name] = {
        "source_path": str(root),
        "built_at": datetime.now().isoformat(),
        "node_count": G.number_of_nodes(),
        "edge_count": G.number_of_edges(),
        "community_count": len(communities),
        "out_dir": str(out_dir),
        "vault_paths": previous_paths,
    }
    _save_registry(cfg, registry)

    result = {
        "status": "ok",
        "name": graph_name,
        "source_path": str(root),
        "node_count": G.number_of_nodes(),
        "edge_count": G.number_of_edges(),
        "community_count": len(communities),
        "god_nodes": gods[:5],
        "graph_json": str(out_dir / "graph.json"),
        "graph_html": str(out_dir / "graph.html"),
        "callflow_html": str(callflow_path) if callflow_path else None,
        "wiki_articles": wiki_count,
    }

    if not file_to_vault:
        result["filed_to_vault"] = None
        return result

    try:
        filed = _write_artifacts_to_vault(vault_manager, graph_name, report_md, wiki_dir, previous_paths)
        # Record what landed in the vault so the next rebuild can clean up after
        # this one — community numbering is not stable across runs.
        registry[graph_name]["vault_paths"] = filed.get("written_paths", [])
        _save_registry(cfg, registry)
        # The full path list belongs in the registry, not in the tool response:
        # it is one line per article, so a mid-sized repo returned hundreds of
        # near-identical strings straight into the calling agent's context.
        filed.pop("written_paths", None)
        result["filed_to_vault"] = filed
    except Exception as e:
        logger.warning("Filing artifacts to vault failed for %s: %s", graph_name, e)
        result["filed_to_vault"] = {"error": str(e)}

    return result


def list_graphs(cfg, name: str | None = None) -> dict:
    """Return the registry of previously built graphs: name, source path, counts, timestamps.

    `vault_paths` is summarised to its length rather than returned. It is the one
    unbounded field in a registry entry — one graph on this machine holds 1441
    entries, and the six together made this 181,666 characters, past the MCP
    tool-result cap, so the tool returned nothing usable at all. Every caller
    (CLI, dashboard, the MCP tool) reads only the counts; graph_preview already
    summarises the same field as `vault_notes_filed`, so this is the shape the
    rest of the module had already settled on.

    Pass `name` to get one graph's full entry, `vault_paths` included — that is
    bounded by a single graph and is how a caller who does want the paths asks.
    """
    registry = _load_registry(cfg)
    if name:
        graph_name = _slugify(name)
        entry = registry.get(graph_name)
        if entry is None:
            return {"error": f"No graph named '{graph_name}'. Call graph_list() for the registry."}
        return {"count": 1, "graphs": {graph_name: entry}}
    return {
        "count": len(registry),
        "graphs": {
            n: {k: v for k, v in e.items() if k != "vault_paths"}
                | {"vault_notes_filed": len(e.get("vault_paths") or [])}
            for n, e in registry.items()
        },
    }


def get_report(cfg, name: str) -> dict:
    """Return the full GRAPH_REPORT.md text for a previously built graph by name."""
    graph_name = _slugify(name)
    report_path = cfg.graphs_dir / graph_name / "GRAPH_REPORT.md"
    if not report_path.exists():
        return {"error": f"No report found for graph '{graph_name}'. Call graph_build first."}
    return {"name": graph_name, "report": report_path.read_text(encoding="utf-8")}


def get_affected(cfg, name: str, query: str, depth: int = 2, relations: list[str] | None = None) -> dict:
    """Blast-radius query: what else is affected if `query` (a file path or symbol
    label) changes? Walks calls/indirect_call/references/imports edges backward
    from the matched node up to `depth` hops. Requires graph_build to have run
    for this name first.
    """
    from delegation_core.graph.affected import (
        DEFAULT_AFFECTED_RELATIONS, affected_nodes, format_affected, load_graph, resolve_seed,
    )

    graph_name = _slugify(name)
    graph_path = cfg.graphs_dir / graph_name / "graph.json"
    if not graph_path.exists():
        return {"error": f"No graph found for '{graph_name}'. Call graph_build first."}

    try:
        G = load_graph(graph_path)
    except Exception as e:
        return {"error": f"Could not load graph: {e}"}

    relation_list = tuple(relations) if relations else DEFAULT_AFFECTED_RELATIONS
    seed = resolve_seed(G, query)
    if seed is None:
        return {"error": f"No unique node match for {query!r} in graph '{graph_name}'."}

    hits = affected_nodes(G, seed, relations=relation_list, depth=depth)
    text = format_affected(G, query, relations=relation_list, depth=depth)
    return {
        "name": graph_name,
        "query": query,
        "seed": seed,
        "depth": depth,
        "relations": list(relation_list),
        "hit_count": len(hits),
        "report": text,
    }


# Formats a built graph can be re-exported into without rebuilding it. The
# heavy work (detect/extract/build/cluster) is already on disk in graph.json;
# these read it back and write one more file, so they cost seconds.
EXPORT_FORMATS = {
    "graphml": ("graph.graphml", "Gephi, yEd, Cytoscape"),
    "svg": ("graph.svg", "static image, embeds anywhere"),
    "cypher": ("graph.cypher", "replay script for Neo4j"),
}


def export_graph(cfg, name: str, fmt: str) -> dict:
    """Re-export an existing graph into another format.

    These exporters shipped with the vendored pipeline and had no caller — the
    same shape as the community-label bug, so they are registered in
    capabilities.py and guarded by tests/test_capability_registry.py.
    """
    fmt = (fmt or "").strip().lower()
    if fmt not in EXPORT_FORMATS:
        return {"error": f"Unknown format '{fmt}'. Valid: {sorted(EXPORT_FORMATS)}"}

    graph_name = _slugify(name)
    out_dir = cfg.graphs_dir / graph_name
    graph_json = out_dir / "graph.json"
    if not graph_json.exists():
        return {"error": f"Graph '{graph_name}' has not been built. Run graph_build first."}

    from delegation_core.graph.affected import load_graph
    from delegation_core.graph.cluster import cluster as cluster_fn, label_communities_by_hub
    from delegation_core.graph.export import to_cypher, to_graphml, to_svg

    filename, opens_in = EXPORT_FORMATS[fmt]
    dest = out_dir / filename
    try:
        G = load_graph(graph_json)
        communities = cluster_fn(G)
        if fmt == "graphml":
            to_graphml(G, communities, str(dest))
        elif fmt == "svg":
            to_svg(G, communities, str(dest),
                   community_labels=label_communities_by_hub(G, communities))
        else:
            to_cypher(G, str(dest))
    except Exception as e:
        logger.error("Export %s of graph %s failed: %s", fmt, graph_name, e)
        return {"error": f"Export failed: {e}"}

    return {"status": "ok", "name": graph_name, "format": fmt,
            "path": str(dest), "opens_in": opens_in,
            "node_count": G.number_of_nodes()}
