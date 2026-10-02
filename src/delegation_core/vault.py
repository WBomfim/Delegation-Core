"""
vault.py — ChromaDB semantic search core.

Delegates embedding setup to embeddings.py (new in v0.2).
VaultManager owns: ChromaDB lifecycle, search, index, reindex, maintenance helpers.

v0.2 improvements:
  - Lazy init with double-checked lock (ABNER) + warm_up() for background pre-loading
  - doc_id parameter on index_note for chunked external ingestion (ABNER)
  - Orphan cleanup in reindex_vault: drops rows whose path no longer exists (SAAD)
  - anonymized_telemetry=False in ChromaDB client (SAAD)
  - _ensure_ready() guard on every public method

v0.3 improvements:
  - Incremental indexing: reindex_vault(force=False) skips notes whose mtime
    has not changed since the last reindex run. State stored in
    {vault}/.chroma_index.json as {rel_path: mtime}.
  - force=True bypasses mtime check (full reindex).

v0.12 improvements:
  - Vault notes are chunked into "<rel path>::chunk_N" rows, the way ingest.py
    has always chunked external files. One row per note meant everything past
    the embedding model's input ceiling was silently unindexed.
  - index_note deletes a note's existing rows before upserting its new ones —
    upsert alone leaves the tail of a shortened note answering searches.
  - .chroma_index.json carries a schema stamp; a mismatch forces one full
    re-index, without which an incremental run would skip every note and the
    chunking change would never reach the index.
"""

import json
import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import gpu, recuperacao
from .config import Config
from .index_lock import close_chroma_client, index_lock_of, reads_index, uses_index
from .embeddings import (
    chunk_text,
    effective_chunk_chars,
    make_bge_embedding_function,
    profile_for,
)
from .linker import frontmatter_aliases

logger = logging.getLogger("vault")

# ── nomes, caminhos e frontmatter ────────────────────────────────────────────
#
# Extraidos para notes.py: sao funcoes puras, sem ChromaDB, e nove modulos as
# importam daqui. Reexportadas para que `from .vault import safe_filename`
# continue valendo — ha teste que falha se alguma sumir.
from . import notes
from .notes import (  # noqa: F401
    _CHUNK_SUFFIX_RE,
    _CODE_SPAN_RE,
    _countable_wikilinks,
    _DATE_PREFIX_RE,
    _EXTRA_CHUNK_SUFFIX_RE,
    _INVALID_FILENAME_CHARS,
    _LEADING_FRONTMATTER_RE,
    _merge_alias,
    _WIKILINK_TARGET_RE,
    client_from_path,
    client_slug,
    compose_note,
    ingested_link_stems,
    link_names_for_stem,
    resolve_in_vault,
    resolve_vault_folder,
    safe_filename,
    unique_note_path,
    yaml_quote_scalar,
    yaml_unquote_scalar,
)

_chroma_write_lock = threading.Lock()
"""Module-level lock guarding all ChromaDB write operations.

run_maintenance_bg() runs asyncio.run() inside a daemon thread, sharing the
same VaultManager and ChromaDB collection as the main event loop. ChromaDB's
embedded client is not thread-safe for concurrent writes. This lock serialises
index_note() calls across both paths.
"""


def _frontmatter_parses(content: str) -> bool:
    """Does this note's frontmatter block survive a real YAML parser?

    True for a note with no frontmatter at all: an absent block is not a broken
    one, and counting it would invent a defect in every plain note.

    PyYAML is a declared dependency of this package (pyproject: pyyaml>=6.0) and
    already imported lazily by sidecar.py and graph/manifest_ingest.py, so this
    adds no new requirement. Imported inside the function for the same reason
    they do it: the health scan is the only caller and it is not on the import
    path of the daemon's startup.
    """
    if not content.startswith("---"):
        return True
    # The same regex compose_note uses to find a caller's block, so "what counts
    # as frontmatter" has one definition here and is not re-guessed.
    m = _LEADING_FRONTMATTER_RE.match(content)
    if m is None:
        # Opens a block and never closes it. Not something YAML can be asked
        # about, and not something to report as a parse failure either: the
        # existing `truncated` metric is what covers a note cut off mid-write.
        return True
    try:
        import yaml
    except ImportError:      # pragma: no cover - declared dependency
        return True
    try:
        yaml.safe_load(m.group(1))
    except Exception:
        return False
    return True


class VaultManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.collection = None
        self.ef = None
        self._initialized = False
        self._init_lock = threading.Lock()
        self._client = None
        self._disk_state: tuple | None = None
        # Registered so the arbiter can drop this manager's `ef` and
        # `collection` when llama.cpp needs the card. Those two are the
        # references that keep BGE's weights alive outside STEF's own cache,
        # and clearing the cache without them frees nothing.
        gpu.register_embeddings_owner(self)

    # ── init ─────────────────────────────────────────────────────────────────

    def _read_disk_state(self) -> tuple | None:
        """Cheap fingerprint of the index on disk: one stat of Chroma's sqlite."""
        try:
            st = (self.cfg.chroma_path / "chroma.sqlite3").stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _reload_if_disk_changed(self):
        """Reopen the collection when another process has written to the index.

        A PersistentClient loads the vector index into memory once and never
        re-reads it, and the write lock above is a threading.Lock — it serialises
        this process and knows nothing about another one. Concurrent writers are
        by design here: the SessionEnd hook fires a detached ``reindex``, the
        SessionStart hook fires ``maintain``, and any CLI use writes to the same
        path while the server runs.

        Measured consequence before this existed: after a CLI ingest, a running
        server answered scope='all' with pre-write content and failed every
        scope-filtered query outright with ChromaDB's "Error finding id", while a
        freshly opened client read the same index perfectly. Only a restart
        cleared it, which made the documented "the transcript is searchable right
        after the session" path the thing that broke search.
        """
        if not self._initialized:
            return
        current = self._read_disk_state()
        if current is None or current == self._disk_state:
            return
        # A thread already holding the index shared would wait on itself for the
        # exclusive lock. The change stays pending and the next call reopens.
        if index_lock_of(self).held_here():
            return
        with index_lock_of(self).exclusive():
            # Another thread may have reopened while this one waited.
            current = self._read_disk_state()
            if not self._initialized or current is None or current == self._disk_state:
                return
            logger.info("Index changed on disk by another process — reopening")
            self._close_client()
            self._initialized = False
            self.collection = None
            self._init()

    def _close_client(self) -> None:
        client, self._client = getattr(self, "_client", None), None
        close_chroma_client(client)

    def _init(self):
        with self._init_lock:
            if self._initialized:
                return
            # An unset vault_path resolves to Path(".") — an existing directory —
            # so no existence check catches it and the whole index materialises
            # under whatever the process's cwd happens to be. Config.load()
            # falls back to defaults on any read error, so this is reachable in
            # production, not just from a hand-written script.
            if not str(self.cfg.vault_path).strip():
                raise ValueError(
                    "vault_path is not configured — refusing to create the index "
                    "in the current working directory. Run the setup wizard, or "
                    "build the Config with Config.load()."
                )
            try:
                import chromadb
                self.cfg.chroma_path.mkdir(parents=True, exist_ok=True)
                # Reuse the embedding function across reopens. Rebuilding it
                # reloads BGE onto the GPU, which a reload triggered by someone
                # else's write must not pay for — and on this machine the GPU is
                # routinely full, so the rebuild can fail where the first one
                # succeeded.
                # Hand the card to BGE before it loads. In agent mode a local
                # task may have left a 12B model resident, and the old answer
                # to that was make_bge_embedding_function's silent fall back to
                # CPU: search kept working and got an order of magnitude slower
                # with nothing in the response to say so.
                gpu.take("embeddings")
                if self.ef is None:
                    # The caps are passed here or nowhere: the config fields exist
                    # but stay inert until they reach the embedding function, and
                    # an uncapped encode is what OOM'd a 16GB card mid-reindex.
                    self.ef = make_bge_embedding_function(
                        self.cfg.bge_model,
                        max_seq_length=self.cfg.embed_max_seq_length,
                        batch_size=self.cfg.embed_batch_size,
                        device=getattr(self.cfg, "embed_device", "auto"))
                recuperacao.antes_de_abrir(self.cfg)  # pode trocar o indice por um novo
                client = chromadb.PersistentClient(
                    path=str(self.cfg.chroma_path),
                    settings=chromadb.Settings(anonymized_telemetry=False),
                )
                try:
                    self._adopt_legacy_collection(client)
                except Exception as e:
                    logger.warning("Legacy collection rename check failed: %s", e)

                self._client = client
                self.collection = client.get_or_create_collection(
                    name=self.cfg.collection_name,
                    embedding_function=self.ef,
                    metadata={"hnsw:space": "cosine"},
                )
            except Exception as e:
                logger.error("ChromaDB/BGE init failed: %s — vault will retry on next call", e)
                return  # do NOT set _initialized; leave it False so _ensure_ready() retries
            # Taken after the open: a write landing mid-open shows up next call.
            self._disk_state = self._read_disk_state()
            self._initialized = True  # only reached on successful init
            stats = self.get_stats()
            recuperacao.depois_de_abrir(self.cfg)
            logger.info("ChromaDB ready — %d chunks across, %d indexed documents; "
                        "%d Markdown notes in vault (%s)", stats["indexed_rows"],
                        stats["indexed_notes"], stats["vault_markdown_files"],
                        self.cfg.collection_name)

    def _adopt_legacy_collection(self, client) -> None:
        """Rename a pre-derivation collection to the model-derived name, if compatible.

        Older installs kept the index under one hardcoded name. The name is
        derived from the embedding model now, so on upgrade those rows went
        invisible and the server quietly built an empty collection beside them.

        The adoption is gated on VECTOR DIMENSION, not merely on the old name
        being present, because the common upgrade is also a model change. A
        vault embedded with bge-base-en-v1.5 lives in a collection literally
        named `vault_bge` and holds 768-dim vectors; adopted under bge-m3's
        1024-dim embedding function, every query and upsert then fails on a
        dimension mismatch — and the original index no longer sits under the
        name a downgrade would look for, so the escape hatch is gone too.
        MODEL_PROFILES annotates `vault_bge` as "historical name — do not
        rename, it holds existing data", and Config.collection_name promises
        the other index survives a model switch. Both hold only while this
        check does.

        An unmeasured model (no profile `dim`) is left alone rather than
        adopted on faith: a wrong adoption is unrecoverable, a skipped one
        costs a reindex.
        """
        existing = [c.name for c in client.list_collections()]
        if self.cfg.collection_name in existing:
            return
        want_dim = profile_for(self.cfg.bge_model).get("dim")
        for legacy_name in ("vault_bge", "vault_custom"):
            if legacy_name not in existing:
                continue
            leg = client.get_collection(legacy_name)
            probe = leg.get(limit=1, include=["embeddings"])
            vectors = probe.get("embeddings")
            have_dim = len(vectors[0]) if vectors is not None and len(vectors) else None
            if have_dim is None:
                logger.info("Legacy collection %s is empty — nothing to adopt", legacy_name)
                return
            if want_dim is None:
                logger.warning(
                    "Legacy collection %s holds %d-dim vectors and %s has no measured "
                    "dimension — not adopting it. Reindex to build %s from scratch.",
                    legacy_name, have_dim, self.cfg.bge_model, self.cfg.collection_name,
                )
                return
            if have_dim != want_dim:
                logger.warning(
                    "Legacy collection %s holds %d-dim vectors but %s produces %d — "
                    "not adopting it. The old index is intact under its own name: "
                    "set bge_model back to reach it, or reindex to rebuild %s.",
                    legacy_name, have_dim, self.cfg.bge_model, want_dim,
                    self.cfg.collection_name,
                )
                return
            logger.info("Adopting legacy collection %s as %s (%d-dim)",
                        legacy_name, self.cfg.collection_name, have_dim)
            leg.modify(name=self.cfg.collection_name)
            return

    def _ensure_ready(self):
        if not self._initialized:
            self._init()
        else:
            self._reload_if_disk_changed()

    def warm_up(self):
        """Start BGE model loading in a background thread before the first tool call."""
        threading.Thread(target=self._init, daemon=True, name="vault-warmup").start()

    # ── search ───────────────────────────────────────────────────────────────

    #: Vault-relative path segment under which graph_build files its wiki articles.
    GENERATED_SEGMENT = "graphs"
    # "<date>-Code Graph Report — <graph>.md", written by graphbridge.
    _REPORT_STEM_RE = re.compile(
        r"^\d{4}-\d{2}-\d{2}-Code Graph Report — (?P<graph>.+)\.md$")

    @classmethod
    def classify_path(cls, rel_path: str) -> tuple[str, str]:
        """Return (kind, graph_name) for a vault-relative note path.

        A generated article lives at ``<folder>/graphs/<graph>/<stem>.md``; the
        graph name is its own metadata field so a search can be pinned to one
        codebase. Anything else is a hand-written note.
        """
        parts = [p for p in str(rel_path).replace("\\", "/").split("/") if p]
        if len(parts) >= 3 and parts[1] == cls.GENERATED_SEGMENT:
            return "generated", parts[2]
        # graph_build's report is machine-written too, but it is filed at the top
        # of its folder on purpose — graphbridge calls it the "discoverable entry
        # point" — so it does not sit under graphs/ and was being graded as a
        # hand-written note. That put 5 reports into the dashboard's knowledge
        # graph and counted their code-derived [[Foo alloc]]-style artifacts as
        # the user's broken links. Recognised by name rather than moved, so the
        # entry point stays where it was designed to be.
        if len(parts) == 2:
            m = cls._REPORT_STEM_RE.match(parts[1])
            if m:
                return "generated", m.group("graph")
        return "note", ""

    @classmethod
    def classify_subkind(cls, rel_path: str, content: str = "") -> str:
        """Return subkind ('transcript', 'chat', 'generated', or 'curated') for a note."""
        kind, _ = cls.classify_path(rel_path)
        if kind == "generated":
            return "generated"
        p_lower = str(rel_path).lower()
        if "transcript-" in p_lower or "session-transcript" in content[:600]:
            return "transcript"
        if "chat-" in p_lower or "claude-ai-export" in content[:600]:
            return "chat"
        return "curated"

    @classmethod
    def note_metadata(cls, rel_path: str, title: str, folder: str, content: str = "") -> dict:
        """Build the metadata row for a vault note, including its search scope and subkind.

        Classmethod because it derives everything from the path — callers that
        only stand in for a VaultManager (write paths, tests) can use it without
        a live ChromaDB collection behind them.
        """
        kind, graph = cls.classify_path(rel_path)
        subkind = cls.classify_subkind(rel_path, content)
        meta = {"title": title, "path": rel_path, "folder": folder, "kind": kind, "subkind": subkind}
        if graph:
            meta["graph"] = graph
        return meta

    @uses_index
    def search(self, query: str, limit: int = 5, scope: str = "all",
               graph: str = "", snippet_chars: int = 800, client: str = "") -> list[dict]:
        """Semantic search, optionally narrowed to one kind of indexed content.

        scope:
          all       — everything (default, previous behaviour)
          notes     — hand-written vault notes only
          generated — graph_build wiki articles only
          external  — ingest_folder'd files only
        graph: restrict to one built graph by name (implies scope='generated').

        Scoping matters once a vault carries machine-generated corpora: after four
        code graphs this vault held 978 generated notes against 179 written by
        hand. Narrowing is pushed into ChromaDB's `where` rather than applied to
        the result list — a first attempt filtered afterwards and returned nothing
        for scope='notes', because all of the top hits were generated and none
        survived. Post-filtering can only ever remove, never reach further down.

        Rows indexed before `kind` existed carry no marker; `reindex(force=True)`
        backfills them. Until then they behave as unscoped and are matched by a
        path fallback.

        Since v0.12 a note occupies one row per chunk, so the k nearest rows can
        be k pieces of the same note — a long transcript would otherwise fill the
        whole result list with itself and push every other note out. Results are
        collapsed by note path, keeping each note's best-scoring chunk (rows come
        back sorted by distance, so the first one seen wins) and that chunk's
        snippet, which is the passage that actually matched.

        The collapse deliberately applies to EXTERNAL rows too, which changes
        behaviour that predates chunking: ingest.py has always split external
        files into ::chunk_N rows, so scope='external' and graph='<name>' could
        previously answer a limit of 5 with five fragments of one file. They now
        return five distinct documents. This is a decision, not a side effect —
        `limit` means "how many documents do you want", and a caller asking for
        five sources and receiving one source five times was never the useful
        reading. Anything wanting the individual passages of a single document
        should read it, not search for it.
        """
        self._ensure_ready()
        if not self.collection:
            return [{"error": "Vault not initialized"}]

        scope = (scope or "all").lower()
        if graph:
            scope = "generated"

        where: dict | None = None
        if graph:
            where = {"graph": graph}
        elif scope == "external":
            where = {"is_external": "true"}
        elif scope in ("notes", "generated"):
            where = {"kind": scope[:-1] if scope == "notes" else scope}

        # The client filter is orthogonal to scope, so it composes with it
        # rather than replacing it — $and, not a second assignment. Normalised
        # through the same function that wrote the values, which is the whole
        # point: a query normalised differently from the index is a filter that
        # matches nothing and reports it as "no results for that client".
        client_key = client_slug(client, getattr(self.cfg, "client_aliases", None))
        if client_key:
            clause = {"client": client_key}
            where = {"$and": [where, clause]} if where else clause

        want = max(limit, 1)
        # Over-fetch unconditionally now, not just when `where` is set: the cut
        # that the query cannot express used to be only the similarity threshold,
        # but chunking added a second one — several rows collapsing into a single
        # note. A floor of 20 keeps small limits (search's default is 5, and
        # merge/relink call it with 5) from being answered entirely out of one
        # long note's chunks; the cap keeps a query off a pathological fetch,
        # since every returned row carries up to vault_chunk_size of document.
        # The ceiling never drops below `want` itself: a caller asking for 100
        # would otherwise be answered from 60 rows, collapse them by path, and
        # receive fewer results than before chunking existed — silently, since
        # nothing in the response says the fetch was capped.
        n_results = min(max(want * 4, 20), max(60, want))

        try:
            kwargs = {"query_texts": [query], "n_results": n_results}
            if where:
                kwargs["where"] = where
            res = self.collection.query(**kwargs)
            hits = []
            seen_paths: set[str] = set()
            docs  = (res.get("documents") or [[]])[0]
            metas = (res.get("metadatas") or [[]])[0]
            dists = (res.get("distances") or [[]])[0]
            for doc, meta, dist in zip(docs, metas, dists):
                # Ghost row defensive guard: skip orphaned HNSW vectors lacking SQLite metadata
                if not meta:
                    continue
                sim = round(1 - dist, 3)

                # Subkind weighting: prioritize curated notes over raw transcripts/dumps
                subkind = meta.get("subkind") or (
                    "external" if str(meta.get("is_external", "")).lower() == "true"
                    else ("generated" if meta.get("kind") == "generated" else "curated")
                )
                weights = getattr(self.cfg, "subkind_weights", {}) or {}
                weight = weights.get(subkind, 1.0)
                effective_sim = round(sim * weight, 3)
                if effective_sim < self.cfg.search_threshold:
                    continue

                # The doc id carries the chunk index; metadata['path'] never
                # does, which is what lets callers feed a hit's path straight
                # back to the filesystem (merger, linker, the dashboard all do).
                path = meta.get("path", "")
                if path:
                    if path in seen_paths:
                        continue
                    seen_paths.add(path)
                kind = meta.get("kind") or (
                    "external" if str(meta.get("is_external", "")).lower() == "true"
                    else self.classify_path(meta.get("path", ""))[0])
                hits.append({
                    "title":      meta.get("title", "Untitled"),
                    "path":       path,
                    "folder":     meta.get("folder", ""),
                    "kind":       kind,
                    "subkind":    subkind,
                    # Returned even when unfiltered: a caller looking at ten
                    # results needs to see that six of them are another client's
                    # before it can know a filter was called for.
                    "client":     meta.get("client", ""),
                    "snippet":    doc[:max(snippet_chars, 0)],
                    "similarity": effective_sim,
                })

            hits.sort(key=lambda h: h["similarity"], reverse=True)
            return hits[:want]
        except Exception as e:
            return [{"error": str(e)}]

    # ── write / index ────────────────────────────────────────────────────────

    @uses_index
    def index_note(self, content: str, metadata: dict, doc_id: str = "") -> bool:
        """Upsert content into ChromaDB. Returns True when the rows landed.

        The return value matters because this method now DELETES a note's
        existing rows before writing its new ones. If the delete lands and the
        upsert then fails, the note has no rows at all — before chunking, a
        failed write merely left the previous row in place, so the failure mode
        was stale content rather than disappearance. reindex_vault stamps a
        note's mtime as current only on True; stamping unconditionally would
        make every later incremental run skip the note it just lost.

        doc_id: explicit ID for chunked external files (IngestManager).
        Defaults to metadata['path'] so vault notes are keyed by their vault-relative path.

        `kind` is derived here rather than trusted from the caller. Of the fifteen
        call sites only graphbridge's and reindex_vault's passed note_metadata();
        every other write path — write_note, vault_update_note, export_session,
        inbox classification, merges, relinking — handed over a bare
        {title, path, folder}. Rows written that way carry no `kind`, and
        search(scope='notes') filters on `kind == "note"` inside ChromaDB, so a
        note was unreachable through the default scope from the moment it was
        written until the next full reindex backfilled it. search()'s docstring
        treats missing `kind` as a legacy condition; these paths kept creating it.

        v0.12 — vault notes are chunked here, and the absence of doc_id is what
        selects that. All 21 call expressions of this method hand over a whole
        note; ingest.py is the single one that has already split its input, and
        it is also the single one that passes an explicit doc_id, so gating on
        `not doc_id` chunks the twenty write paths that need it without touching
        any of them. Before this, one note was one row: bge-m3 stops reading at
        8192 tokens, so a 122k-token graph report was 6.7% indexed and the rest
        of it could not be found by any query.
        """
        self._ensure_ready()
        if not self.collection:
            # Silent until 2026-09-02, and it is the worst of the two failure
            # paths: every write path in the project reaches here, twenty of the
            # twenty-one discard the return value, and the note simply stops
            # existing as far as search is concerned. Not a warning — an error.
            logger.error(
                "index_note: no collection, %r was NOT indexed and is invisible "
                "to search until the next reindex", metadata.get("path", "<sem path>"),
            )
            return False
        # Only vault-relative paths can be classified. External content scopes on
        # is_external, and an absolute path must never be stamped: classify_path
        # grades anything it cannot recognise as hand-written, which would file
        # ingested source files under scope='notes'. Rows that reach here absolute
        # and unmarked stay unscoped — see inject_backlinks, which re-indexes with
        # a bare metadata dict and drops the is_external marker it was given.
        raw_path = str(metadata.get("path", ""))
        # Both flavours: PurePosixPath misses "C:\...", PureWindowsPath misses "/...".
        absolute = (PurePosixPath(raw_path).is_absolute()
                    or PureWindowsPath(raw_path).is_absolute())
        classifiable = (
            "kind" not in metadata
            and bool(raw_path)  # classify_path("") grades as "note"
            and str(metadata.get("is_external", "")).lower() != "true"
            and not absolute
        )
        if classifiable:
            kind, graph = self.classify_path(metadata.get("path", ""))
            subkind = metadata.get("subkind") or self.classify_subkind(metadata.get("path", ""), content)
            metadata = {**metadata, "kind": kind, "subkind": subkind}
            if graph:
                metadata["graph"] = graph

        # A caller-supplied client is normalised and kept — that is ingest's
        # path-derived value, which must not be second-guessed by parsing
        # frontmatter out of a PDF's extracted text.
        aliases = getattr(self.cfg, "client_aliases", None) or {}
        if "client" in metadata:
            slug = client_slug(metadata["client"], aliases)
            metadata = {**metadata, "client": slug} if slug else {
                k: v for k, v in metadata.items() if k != "client"}

        # A vault note: no caller-supplied id, and a relative path to key the
        # chunks by. Absolute and is_external rows are left alone — those belong
        # to ingest.py, which owns their chunk ids, and the where-delete below
        # would reap the siblings it just wrote.
        chunkable = (
            not doc_id
            and bool(raw_path)
            and not absolute
            and str(metadata.get("is_external", "")).lower() != "true"
        )
        # Promote `client:` and all declared frontmatter keys from the note's own frontmatter,
        # for any vault note carrying content. Gated on `chunkable`, NOT on `classifiable`:
        # classifiable is false the moment a caller supplies `kind`, and
        # note_metadata always supplies it: so gating there meant reindex and
        # every write path that builds metadata properly promoted nothing, while
        # the sloppier callers did.
        if chunkable and content:
            fm = self._parse_frontmatter(content)
            if "client" not in metadata and "client" in fm:
                declared = fm.get("client", "")
                slug = client_slug(declared, aliases)
                if slug:
                    metadata = {**metadata, "client": slug}

            reserved = {"path", "kind", "subkind", "folder", "title", "graph", "chunk", "total_chunks", "is_external", "client"}
            extra_meta = {}
            for k, v in fm.items():
                if k not in reserved and k not in metadata:
                    if isinstance(v, (str, int, float, bool)):
                        extra_meta[k] = v
                    elif isinstance(v, (list, tuple)):
                        extra_meta[k] = ", ".join(str(x) for x in v)
                    elif v is not None:
                        extra_meta[k] = str(v)
            if extra_meta:
                metadata = {**metadata, **extra_meta}

        if chunkable:
            # The configured size is in characters, the model's limit in tokens;
            # reconcile them here or a chunk sized for one model gets quietly
            # truncated by another (4000 chars against bge-base's 512-token
            # window loses roughly half of every chunk).
            chunks = chunk_text(content,
                                max_chars=effective_chunk_chars(
                                    self.cfg.bge_model,
                                    self.cfg.vault_chunk_size,
                                    self.cfg.embed_max_seq_length),
                                overlap=self.cfg.vault_chunk_overlap)
            total = str(len(chunks))
            # ::chunk_0 even when there is exactly one chunk, unlike ingest.py's
            # bare-path special case. If the key shape depended on the content,
            # every cleanup path would need two rules and a note crossing the
            # one-chunk boundary would leave the other shape behind.
            ids = [f"{raw_path}::chunk_{i}" for i in range(len(chunks))]
            metas = [{**metadata, "chunk": str(i), "total_chunks": total}
                     for i in range(len(chunks))]
        else:
            ids = [doc_id or metadata.get("path", str(datetime.now().timestamp()))]
            chunks = [content]
            metas = [metadata]

        try:
            with _chroma_write_lock:
                if chunkable:
                    # upsert replaces, it never removes. A note edited from 12
                    # chunks down to 3 would keep ::chunk_3..11 verbatim from the
                    # old revision — text that exists in no file, answering
                    # searches forever, and invisible to the orphan sweep because
                    # the note itself is still on disk. organizer's heal pass
                    # exists to shorten notes, so this is the common case, not the
                    # exotic one. Failure here is logged rather than fatal: a
                    # stale sibling is worse than nothing, but not indexing the
                    # note at all is worse still.
                    try:
                        self.collection.delete(where={"path": raw_path})
                    except Exception as e:
                        logger.warning("Stale-chunk cleanup failed for %s: %s", raw_path, e)
                self.collection.upsert(ids=ids, documents=chunks, metadatas=metas)
                # Our own write moves the fingerprint; adopt it, or the next call
                # reads it as a foreign change and reopens on every single write.
                self._disk_state = self._read_disk_state()
        except Exception as e:
            # Naming the note is the whole difference between a log line someone
            # can act on and one that only proves something went wrong. The
            # delete above may already have landed, so this note can be on disk
            # with no rows at all.
            logger.error("index_note: %r was NOT indexed (%s: %s) — it is on disk "
                         "and invisible to search", raw_path, type(e).__name__, e)
            return False
        return True

    @uses_index
    def delete_notes(self, rel_paths: list[str]) -> int:
        """Drop notes from ChromaDB and the incremental index state by vault-relative path.

        Counterpart to index_note: removing a note's file without this leaves
        stale rows that keep surfacing in search until the next full reindex runs
        its orphan sweep.

        Deletion goes through `where={"path": ...}` rather than a list of ids
        because since v0.12 a note is many rows and only the note itself knows
        how many: deleting id "Reference/a.md" now removes nothing at all, and
        reconstructing "Reference/a.md::chunk_N" needs a chunk count this method
        is never given. The metadata path is the one thing every row of a note
        shares — including the pre-v0.12 whole-note row, which the same call
        reaps. One `$in` query covers the whole batch; graph_build passes the
        entire stale wiki of a graph here, which is hundreds of paths.

        Returns the number of note paths submitted for deletion (not rows: the
        row count is not knowable without a second round-trip, and every caller
        uses this as "how many notes did I ask to drop").
        """
        self._ensure_ready()
        if not self.collection or not rel_paths:
            return 0
        paths = list(rel_paths)
        try:
            with _chroma_write_lock:
                # Batched like every other bulk delete here: one `$in` holding
                # graph_build's several hundred paths is exactly the shape that
                # trips SQLite's variable ceiling, and the failure is invisible —
                # the except below logs and returns 0, while graphbridge and
                # notewriter both discard the return, so a whole stale graph wiki
                # would stay searchable with nobody told.
                batch_size = 5000
                for i in range(0, len(paths), batch_size):
                    self.collection.delete(
                        where={"path": {"$in": paths[i:i + batch_size]}}
                    )
                self._disk_state = self._read_disk_state()
        except Exception as e:
            logger.warning("Delete error: %s", e)
            return 0
        def sem_os_apagados(estado):
            for p in rel_paths:
                estado.pop(p, None)
            return estado
        self._update_index_state(sem_os_apagados)
        return len(rel_paths)

    # ── incremental index state ───────────────────────────────────────────────

    #: Row shape the saved mtimes certify. 1 = one row per note (pre-v0.12),
    #: 2 = one row per ::chunk_N. Bump this whenever a change makes existing
    #: rows wrong rather than merely stale — reindex_vault discards the whole
    #: state on a mismatch, which turns the next ordinary incremental run into
    #: one full re-embed and then stamps the new number, so it costs exactly one
    #: pass and never re-fires. Without it the chunking fix ships inert: every
    #: note's mtime still matches, so `delegation-core reindex` writes nothing.
    #: 3 (v0.13): rows gained a `client` key. Existing rows do not have it, so
    #: a client filter over an unmigrated index matches nothing and looks like
    #: an empty vault — the bump is what makes the feature real rather than
    #: merely present.
    _INDEX_SCHEMA = 3
    #: Reserved key inside .chroma_index.json. Can never collide with a note:
    #: keys there are vault-relative paths, which always end in ".md".
    _SCHEMA_KEY = "__schema__"

    def _index_state_path(self) -> Path:
        return notes.caminho_do_estado(self.cfg.vault)

    def _load_index_state(self) -> dict[str, float]:
        return notes.carregar_estado(self._index_state_path())

    def _save_index_state(self, state: dict[str, float]):
        notes.gravar_estado(self._index_state_path(), state)

    def _update_index_state(self, modificar) -> dict:
        """Ler-modificar-escrever do estado do indice sob trava.

        Todo escritor deste arquivo passa por aqui. Ver `notes.atualizar_estado`
        para o defeito medido que exigiu a trava: escrita atomica sozinha nao
        impede que o ultimo a gravar apague o carimbo do outro.
        """
        return notes.atualizar_estado(self._index_state_path(), modificar)


    def stamp_indexed(self, rel_paths: list[str]) -> int:
        """Record that these notes are current in the index. Returns how many.

        `reindex_vault(force=False)` skips a note whose mtime matches the value
        recorded in `.chroma_index.json`, and it is the ONLY writer of that file
        besides `delete_notes`. Every other path that indexes a note leaves it
        unstamped, so the next "incremental" run re-embeds a note that is
        already correctly indexed and has not changed.

        Measured on this vault on 2026-09-02: the Reference folder held 8.262
        notes on disk with 3.469 stamped. graph_build writes its report and one
        article per community straight through `index_note`, thousands at a
        time, and never stamps any of them. So 4.793 generated notes were
        re-embedded on EVERY incremental reindex, turning a run that had five
        genuinely new notes to index into a near-full rebuild that held the GPU
        for around twenty-five minutes.

        That cost is not academic: the SessionEnd hook fires
        `delegation-core reindex` after every Claude Code session, and this
        machine carries a `no_auto_reindex` sentry file that switches that off
        entirely — which is also why hook-written transcripts stopped reaching
        the index.

        Bulk on purpose. Stamping inside `index_note` would rewrite the whole
        state file once per note, which during a reindex of 8.581 notes is 8.581
        full-file writes. One read, one write, however many paths.

        A path whose file is gone is skipped rather than stamped: stamping a
        mtime for a file that does not exist would make the next reindex skip a
        note it needs to reap.
        """
        if not rel_paths:
            return 0
        novos: dict[str, float] = {}
        for rel in rel_paths:
            try:
                novos[rel] = (self.cfg.vault / rel).stat().st_mtime
            except OSError:
                continue
        if novos:
            self._update_index_state(lambda estado: {**estado, **novos})
        return len(novos)

    @reads_index
    def _unindexed_notes(self, notes: list[dict]) -> list[dict]:
        """Notes that exist on disk and have no rows in the index.

        This is the failure this project keeps having and keeps not seeing.
        `index_note()` returns False when the write does not land, and of its
        21 call expressions exactly one — `reindex_vault` — reads that return.
        The other twenty discard it, so a note that fails to index stays on
        disk, drops out of every search, and nothing anywhere says so. One of
        the two failure paths inside `index_note` did not even log.

        Measured on this vault on 2026-09-02: 8.581 notes on disk against 8.576
        distinct indexed paths. The five missing were three Daily transcripts
        and two session transcripts, none of them stamped in
        `.chroma_index.json` and none of them named in any log. Finding them
        took a hand-written sqlite query against the Chroma store, which is
        exactly the kind of throwaway script `vault_health_detail` exists to
        make unnecessary.

        Counted the same way as the rest of health: collected in the same pass
        that produces the number, so the list and the count cannot disagree.
        The reverse direction — rows whose file is gone — is the orphan sweep in
        `reindex_vault`, which is a different question with a different fix.
        """
        if not self.collection:
            return []
        try:
            rows = self._paged_get(include=["metadatas"])
        except Exception as e:
            # Health must degrade to "cannot tell" rather than to "all fine":
            # returning [] here would report `unindexed: 0` for an index that
            # could not be read at all, which is the fabricated-value failure
            # this whole function exists to catch.
            logger.warning("Could not read indexed paths for the health scan: %s", e)
            return [{"rel": "", "folder": "", "error": f"index unreadable: {e}"}]

        indexed: set[str] = set()
        for meta in rows.get("metadatas") or []:
            if not meta:
                continue
            # External rows are keyed by absolute path and are not vault notes;
            # comparing them against a vault walk would report every ingested
            # file as missing.
            if str(meta.get("is_external", "")).lower() == "true":
                continue
            path = meta.get("path")
            if path:
                indexed.add(str(path))

        return [{"rel": n["rel"], "folder": n["folder"], "stem": n["stem"]}
                for n in notes if n.get("rel") and n["rel"] not in indexed]

    @reads_index
    def _paged_get(self, limit: int = 5000, **kwargs) -> dict:
        """Safely fetch all matching rows from ChromaDB in batches to prevent SQLite variable limits."""
        if not self.collection:
            return {"ids": [], "metadatas": []}
        offset = 0
        all_ids = []
        all_metas = []
        try:
            while True:
                chunk = self.collection.get(limit=limit, offset=offset, **kwargs)
                ids = chunk.get("ids") or []
                if not ids:
                    break
                all_ids.extend(ids)
                if "metadatas" in kwargs.get("include", []):
                    all_metas.extend(chunk.get("metadatas") or [])
                if len(ids) < limit:
                    break
                offset += len(ids)
            return {"ids": all_ids, "metadatas": all_metas}
        except TypeError:
            # Fallback for test mocks or custom wrappers
            return self.collection.get(**kwargs)

    @uses_index
    def reindex_vault(self, force: bool = False) -> int:
        """Reindex markdown notes in configured vault folders.

        force=False (default): skips notes whose mtime matches the last recorded
        value in {vault}/.chroma_index.json — incremental, fast on large vaults.
        force=True: re-indexes every note regardless of mtime.

        Also removes orphan ChromaDB rows whose vault path no longer exists on
        disk. Externally ingested rows are never touched.
        """
        self._ensure_ready()
        if not self.collection:
            return 0

        state = {} if force else self._load_index_state()
        descartou_o_estado = bool(force)
        # A state file written under an older row shape certifies nothing about
        # the rows in ChromaDB now, so the mtimes in it must not be allowed to
        # skip anything. Popped either way, so the reserved key is never walked
        # as if it were a note path; re-stamped at the save below, which is what
        # keeps this a one-time cost. Old files carry no stamp at all and read
        # as 0 — the upgrade case this exists for.
        if state.pop(self._SCHEMA_KEY, 0) != self._INDEX_SCHEMA:
            if state:
                logger.info("Index state schema changed — re-indexing every note once")
            state = {}
            descartou_o_estado = True
        count = 0
        skipped = 0
        on_disk: set[str] = set()

        for folder in self.cfg.vault_folders:
            folder_path = self.cfg.vault / folder
            if not folder_path.exists():
                continue
            # rglob (not glob): index notes in subfolders too. With a
            # non-recursive glob, `on_disk` omits subfolder notes, so the orphan
            # sweep below deletes every already-indexed subfolder note from
            # ChromaDB — silently collapsing the search index on each reindex.
            # (0.5.0 used rglob here; v6.0/6.1 regressed it to glob.)
            for f in folder_path.rglob("*.md"):
                rel = str(f.relative_to(self.cfg.vault))
                on_disk.add(rel)
                mtime = f.stat().st_mtime
                if not force and abs(state.get(rel, 0) - mtime) < 0.001:
                    skipped += 1
                    continue
                try:
                    content = f.read_text(encoding="utf-8")
                    fm = self._parse_frontmatter(content)
                    title = fm.get("title") or f.name[:-3]
                    # `content` reaches note_metadata so classify_subkind can read
                    # the frontmatter signals, not just the path. A transcript
                    # whose nature shows only in `type: session-transcript` is
                    # graded transcript when written and would be graded curated
                    # here — the same note ranking differently depending on which
                    # write path touched it last.
                    ok = self.index_note(content, self.note_metadata(rel, title, folder, content))
                    if ok:
                        state[rel] = mtime
                        count += 1
                    else:
                        # Leave it unstamped. Stamping a note whose rows were just
                        # deleted but not rewritten hides it from every later
                        # incremental run — the note would stay missing from search
                        # until someone thought to force a full reindex.
                        logger.warning("Index write failed for %s — left unstamped for retry", rel)
                except Exception as e:
                    logger.warning("Could not index %s: %s", f.name, e)

        try:
            with _chroma_write_lock:
                # get() and delete() share the lock — prevents newly-indexed notes from
                # being identified as orphans between the get() and delete() calls.
                # The `on_disk` snapshot itself is stale by the time we get here: it was
                # built by the folder walk above, which can take a while, and a
                # concurrent write_note()/vault_update_note() on the main thread can
                # index a brand-new note in that window. Re-check the filesystem for
                # each orphan candidate rather than trusting on_disk alone, or a note
                # written mid-reindex gets its just-created ChromaDB row deleted here
                # (file survives, but silently drops out of search_vault).
                #
                # Vault-vs-external is decided on the path, not on '::' as it
                # was before v0.12: now that vault notes are chunked too, "'::'
                # in the id" exempts every note in the vault from the sweep and
                # deleted notes stay searchable forever. It is also not decided
                # on `kind` — rows written before that field existed carry none,
                # and a destructive sweep must not read "no marker" as "not a
                # note". What holds regardless of vintage is that ingest keys its
                # rows by absolute source path while a vault note is always
                # relative, which is the same test index_note uses to decide
                # whether a path can be classified at all.
                existing = self._paged_get(include=["metadatas"])
                existing_ids = existing.get("ids") or []
                existing_metas = existing.get("metadatas") or []
                orphans = []
                orphan_bases = set()
                for pos, i in enumerate(existing_ids):
                    meta = (existing_metas[pos] if pos < len(existing_metas) else None) or {}
                    if str(meta.get("is_external", "")).lower() == "true":
                        continue
                    base = _CHUNK_SUFFIX_RE.sub("", i)
                    if not base or (PurePosixPath(base).is_absolute()
                                    or PureWindowsPath(base).is_absolute()):
                        continue
                    if base in on_disk or (self.cfg.vault / base).exists():
                        continue
                    orphans.append(i)
                    orphan_bases.add(base)
                if orphans:
                    # Delete in batches of 5000 to prevent SQLite variable limits
                    batch_size = 5000
                    for b in range(0, len(orphans), batch_size):
                        self.collection.delete(ids=orphans[b:b + batch_size])
                    self._disk_state = self._read_disk_state()
                # Remove orphan entries from saved state — keyed by the note's
                # path, so popping the chunk id would leave every entry behind.
                for o in orphan_bases:
                    state.pop(o, None)
                logger.info("Reindex dropped %d orphan rows (%d notes)",
                            len(orphans), len(orphan_bases))
        except Exception as e:
            logger.warning("Orphan cleanup failed: %s", e)

        # Mesclar, e nao sobrescrever: a passagem acima leva minutos, e um
        # `stamp_indexed` de outra thread nesse meio tempo era perdido aqui.
        # Em `force` (ou esquema trocado) o descarte e deliberado, entao o
        # estado em disco e ignorado de proposito.
        def resultado_da_passagem(estado):
            base = {} if descartou_o_estado else estado
            base.update(state)
            base[self._SCHEMA_KEY] = self._INDEX_SCHEMA
            return base
        self._update_index_state(resultado_da_passagem)
        if skipped:
            logger.info("Reindex: %d indexed, %d unchanged (skipped)", count, skipped)
        return count

    # ── maintenance helpers ───────────────────────────────────────────────────

    def count_notes(self, folder: str) -> int:
        """Total notes in a folder, ignoring any list limit.

        list_notes() truncates with a bare slice, so its caller cannot tell a
        folder of 20 notes from a folder of 3715 showing its newest 20. Every
        surface that lists notes reports this alongside the truncated list.
        """
        folder_path = self.cfg.vault / folder
        if not folder_path.exists():
            return 0
        return sum(1 for _ in folder_path.rglob("*.md"))

    def list_notes(self, folder: str, limit: int = 20) -> list[dict]:
        """List notes in a folder (including subfolders) sorted newest-first by mtime.

        rglob, not glob: reindex_vault() already made this exact fix (see its own
        comment) because a non-recursive glob silently omits subfolder notes. This
        method had the same bug independently — found while building the
        dashboard's notes-browser endpoint, which showed 0 notes for a folder that
        actually had its notes nested one level down.
        """
        folder_path = self.cfg.vault / folder
        if not folder_path.exists():
            return []
        files = sorted(folder_path.rglob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
        return [self._note_row(f) for f in files[:limit]]

    def _note_row(self, f: Path) -> dict:
        """Title/date/path/size for one note, reading only its frontmatter head."""
        title, date = f.name[:-3], ""
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        try:
            for line in f.read_text(encoding="utf-8").splitlines()[:8]:
                if line.startswith("title:"):
                    title = yaml_unquote_scalar(line.split(":", 1)[1])
                elif line.startswith("date:"):
                    date = line.split(":", 1)[1].strip()
        except Exception as e:
            logger.warning("Could not read frontmatter from %s: %s", f.name, e)
        return {"title": title, "date": date,
                "path": str(f.relative_to(self.cfg.vault)), "size_bytes": size}

    def list_directories(self) -> list[dict]:
        """Every directory under a configured folder that holds notes.

        The browser used to list only the configured top-level folders, so a
        vault where 3661 of 3878 notes sit three levels down showed them as if
        they were loose in their top folder, with no way to reach the subtree.
        Returning the real shape is cheap here: this vault has 25 such
        directories.
        """
        out = []
        for folder in self.cfg.vault_folders:
            root = self.cfg.vault / folder
            if not root.exists():
                continue
            seen: dict[str, int] = {}
            for f in root.rglob("*.md"):
                rel = str(f.parent.relative_to(self.cfg.vault))
                seen[rel] = seen.get(rel, 0) + 1
            for rel in sorted(seen):
                out.append({"path": rel,
                            "name": rel.split("/")[-1],
                            "depth": rel.count("/"),
                            "count": seen[rel]})
        return out

    def list_notes_in(self, dir_rel: str, offset: int = 0, limit: int = 200) -> dict:
        """Notes directly inside one directory — not its subdirectories.

        Paginated because a single directory here holds 2711 notes; the caller
        gets `total` and `has_more` so a page is never mistaken for the lot.
        """
        target = resolve_in_vault(self.cfg.vault, dir_rel)
        if target is None:
            return {"error": f"Path outside the vault: {dir_rel}"}
        if not target.is_dir():
            return {"error": f"Not a directory: {dir_rel}"}

        files = sorted(target.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
        page = files[offset:offset + limit]
        return {"dir": dir_rel, "total": len(files), "offset": offset,
                "limit": limit, "has_more": offset + len(page) < len(files),
                "notes": [self._note_row(f) for f in page]}

    def find_notes(self, query: str, limit: int = 30) -> list[dict]:
        """Literal title/path search — no embeddings, no similarity threshold.

        search() is semantic and answers "what is about X". It cannot reliably
        answer "open the note called X": searching this vault for the exact
        title of a note written minutes earlier did not return it in the top 3,
        and a one-word title matched at 0.57 against a 0.55 cutoff. Exact recall
        needs literal matching, so this walks filenames instead.

        Ranked: exact stem, then prefix, then substring in the stem, then
        anywhere in the path.
        """
        q = (query or "").strip().lower()
        if not q:
            return []
        scored = []
        for folder in self.cfg.vault_folders:
            root = self.cfg.vault / folder
            if not root.exists():
                continue
            for f in root.rglob("*.md"):
                stem = f.stem.lower()
                rel = str(f.relative_to(self.cfg.vault))
                if stem == q:
                    rank = 0
                elif stem.startswith(q):
                    rank = 1
                elif q in stem:
                    rank = 2
                elif q in rel.lower():
                    rank = 3
                else:
                    continue
                scored.append((rank, len(stem), rel, f))
        scored.sort(key=lambda t: (t[0], t[1], t[2]))
        return [dict(self._note_row(f), match_rank=rank)
                for rank, _, _, f in scored[:limit]]

    def resolvable_link_targets(self) -> dict[str, Path]:
        """Every name a `[[wikilink]]` in this vault can legitimately resolve to.

        Wider than "stems of notes in vault_folders", in two ways that both
        produce false "broken link" reports when missed:

        - **Frontmatter aliases.** A note declaring `aliases: [Command Center]`
          is reachable by that name.
        - **Notes outside the managed folders.** MEMORY.md at the vault root and
          anything staged in _processed/ resolve fine for a reader; only grading
          is scoped to vault_folders.

        vault_health() has always resolved this way. note_links() did not when it
        was written, and would have labelled 9 live targets in this vault as
        missing — the exact misreport the backlinks panel exists to avoid. One
        method now, so the panel and the health count cannot disagree.
        """
        targets: dict[str, Path] = {}
        for f in self.cfg.vault.rglob("*.md"):
            if any(part.startswith(".") for part in f.relative_to(self.cfg.vault).parts):
                continue
            for name in link_names_for_stem(f.name[:-3]):
                targets.setdefault(name, f)
        for folder in self.cfg.vault_folders:
            root = self.cfg.vault / folder
            if not root.exists():
                continue
            for f in root.rglob("*.md"):
                try:
                    content = f.read_text(encoding="utf-8")
                except OSError:
                    continue
                for alias in frontmatter_aliases(content):
                    targets.setdefault(alias.lower(), f)
        return targets

    def note_links(self, rel_path: str) -> dict:
        """Inbound and outbound wikilinks for one note.

        The server has always computed backlinks — linker.inject_backlinks writes
        them into the note body on every write — but nothing exposed the relation
        itself, so a reader could only see whatever text happened to be there.

        Uses `_countable_wikilinks`, the same filter vault_health's broken-link
        count uses, rather than linker.existing_targets. The distinction is not
        cosmetic: existing_targets is a bare `[[...]]` regex, so it counts shell
        test syntax and imported path references as links. Measured on this
        vault, the naive reading gives 606 links / 176 broken; the filtered one
        gives 527 / 63.

        Broken outbound links are returned rather than dropped — 63 of them exist
        and a panel that silently omitted them would misreport the note.
        """
        target = resolve_in_vault(self.cfg.vault, rel_path)
        if target is None:
            return {"error": f"Path outside the vault: {rel_path}"}
        if not target.is_file():
            return {"error": f"Not a note: {rel_path}"}

        by_stem = self.resolvable_link_targets()
        own_stem = target.stem.lower()
        own_text = target.read_text(encoding="utf-8", errors="ignore")
        outbound = []
        for link in _countable_wikilinks(own_text):
            hit = by_stem.get(link.strip().lower())
            outbound.append({"target": link.strip(),
                             "path": str(hit.relative_to(self.cfg.vault)) if hit else None,
                             "broken": hit is None})

        # Iterate distinct files, not by_stem keys: an aliased note appears under
        # every one of its names, which listed it once per alias.
        # A link may address this note by stem or by any alias it declares.
        own_names = link_names_for_stem(own_stem) | {
            a.lower() for a in frontmatter_aliases(own_text)}

        # Iterate distinct files, not by_stem keys: an aliased note appears under
        # every one of its names, which listed it once per alias.
        inbound = []
        for f in dict.fromkeys(by_stem.values()):
            if f == target:
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if any(t.strip().lower() in own_names for t in _countable_wikilinks(content)):
                inbound.append(self._note_row(f))

        inbound.sort(key=lambda n: n["title"].lower())
        return {"path": rel_path, "inbound": inbound, "outbound": outbound,
                "inbound_count": len(inbound),
                "broken_count": sum(1 for o in outbound if o["broken"])}

    def inbox_status(self) -> dict:
        """Return what is waiting in the vault _inbox folder."""
        from .extractor import SUPPORTED, format_label
        inbox = self.cfg.vault / "_inbox"
        if not inbox.exists():
            return {"count": 0, "files": [], "unsupported": [], "inbox_path": str(inbox)}

        supported, unsupported = [], []
        for f in sorted(inbox.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True):
            if not f.is_file():
                continue
            entry = {
                "name": f.name,
                "format": format_label(f),
                "size_bytes": f.stat().st_size,
                "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            }
            (supported if f.suffix.lower() in SUPPORTED else unsupported).append(entry)

        return {"count": len(supported), "files": supported,
                "unsupported": unsupported, "inbox_path": str(inbox)}

    def find_notes_by_stem(self, note_name: str) -> list[Path]:
        """Find notes whose filename stem contains note_name (case-insensitive).

        rglob, not glob: same subfolder-visibility fix as reindex_vault/list_notes
        (see their comments) — a non-recursive glob here made read_note,
        vault_update_note, and vault_find_similar all silently miss any note
        nested one level down (e.g. research/Client/...).
        """
        matches: list[Path] = []
        for folder in self.cfg.vault_folders:
            folder_path = self.cfg.vault / folder
            if not folder_path.exists():
                continue
            for f in folder_path.rglob("*.md"):
                if note_name.lower() in f.name[:-3].lower():
                    matches.append(f)
        if len(matches) > 1:
            logger.warning(
                "Ambiguous note name '%s' matches %d notes — using %s",
                note_name, len(matches), matches[0].relative_to(self.cfg.vault),
            )
        return matches

    @uses_index
    def find_similar(self, note_name: str, threshold: float = 0.80, limit: int = 5) -> list[dict]:
        """Find notes semantically similar to the given note."""
        self._ensure_ready()
        if not self.collection:
            return [{"error": "Vault not initialized"}]
        matches = self.find_notes_by_stem(note_name)
        if not matches:
            return [{"error": f"Note not found: {note_name}"}]
        f = matches[0]
        try:
            source_content = f.read_text(encoding="utf-8")
        except Exception as e:
            return [{"error": f"Could not read note: {note_name} — {e}"}]
        source_path = str(f.relative_to(self.cfg.vault))
        hits = self.search(source_content[:1000], limit=limit + 1)
        return [h for h in hits if h.get("path") != source_path and h.get("similarity", 0) >= threshold]

    def update_note(self, note_name: str, append_content: str) -> dict:
        """Append content to an existing note and re-index it."""
        matches = self.find_notes_by_stem(note_name)
        if not matches:
            return {"error": f"Note not found: {note_name}"}
        f = matches[0]
        folder = f.parent.name
        try:
            existing = f.read_text(encoding="utf-8")
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            updated = existing + f"\n\n---\n*Updated {stamp}*\n\n{append_content}"
            f.write_text(updated, encoding="utf-8")
            rel = str(f.relative_to(self.cfg.vault))
            fm = self._parse_frontmatter(updated)
            title = fm.get("title") or f.name[:-3]
            self.index_note(updated, {"title": title, "path": rel, "folder": folder})
            return {"status": "ok", "path": rel, "appended_chars": len(append_content)}
        except Exception as e:
            return {"error": str(e)}

    # ── quality health ────────────────────────────────────────────────────────

    def _parse_frontmatter(self, content: str) -> dict:
        """Extract key:value pairs from the first YAML frontmatter block."""
        fm = {}
        if content.startswith("---\n"):
            close = content.find("\n---\n", 4)
            if close != -1:
                for line in content[4:close].splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)   # split(1) preserves colons in values
                        fm[k.strip()] = yaml_unquote_scalar(v)
        return fm

    def get_health_summary(self, force: bool = False) -> dict:
        """Scan vault frontmatter for quality issues. Cached 5 min in ~/.delegation_core/vault_health.json."""
        from .config import vault_health_cache
        cache_path = vault_health_cache()
        now = datetime.now().timestamp()

        if not force and cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                # Keyed by vault: the cache path is fixed, so without this a
                # second vault — another profile, or a dashboard sidecar pointed
                # elsewhere — is served the first one's numbers for five minutes.
                # Found when two tests over different temp vaults returned
                # identical health: the second never ran.
                same_vault = cached.get("vault_path") == str(self.cfg.vault)
                if same_vault and now - cached.get("computed_at_ts", 0) < 300:
                    cached.pop("computed_at_ts", None)
                    cached.pop("vault_path", None)
                    return cached
            except Exception:
                pass

        # O vault inteiro fora do ar responde igual a um vault vazio, e nao pode.
        #
        # MEDIDO em 04/09/2026 apontando vault_path para um diretorio que nao
        # existe: total_notes 0, needs_repair 0, truncated 0, orphans 0,
        # broken_links 0, unindexed 0, e NENHUMA ressalva. Quem le heartbeat()
        # conclui que o vault esta saudavel e vazio -- e o mesmo desfecho de um
        # vault de 8.593 notas que acabou de ser desmontado, renomeado, ou que
        # mora num drive que nao subiu.
        #
        # `_unindexed_notes` ja carrega a regra no proprio docstring: "Health
        # must degrade to 'cannot tell' rather than to 'all fine'". Ela valia
        # para o indice ilegivel e nao valia para o vault ausente.
        #
        # So a RAIZ e alarme: uma PASTA configurada que nao existe e normal (um
        # vault que nao usa Tools/ nao tem Tools/), e o passe abaixo ja a pula.
        if not self.cfg.vault.is_dir():
            motivo = ("does not exist" if not self.cfg.vault.exists()
                      else "is not a directory")
            resultado = {
                "total_notes": 0, "needs_repair": 0, "truncated": 0,
                "orphans": 0, "generated_notes": 0, "broken_links": 0,
                "unindexed": 0, "malformed_frontmatter": 0,
                "vault_unreadable": f"{self.cfg.vault} {motivo}",
                "computed_at": datetime.now().isoformat(),
            }
            self._last_health_detail = {"_vault": str(self.cfg.vault), **resultado}
            return resultado

        threshold = getattr(self.cfg, "quality_threshold", 0.50)
        # Compared case-insensitively below: a vault whose folder is "Sessions"
        # would otherwise skip nothing and count every session note as an orphan.
        skip_orphan = {"sessions"}

        # ── Pass 1: read every note once; collect resolution keys + per-note data ──
        # A link resolves against the union of filename stems AND Obsidian
        # frontmatter aliases (v6) — mirroring how Obsidian itself resolves.
        resolvable: set[str] = set()      # lowercased stems + aliases
        notes: list[dict] = []
        for folder in self.cfg.vault_folders:
            fp = self.cfg.vault / folder
            if not fp.exists():
                continue
            # rglob (not glob): notes live in subfolders too (e.g.
            # research/Client/…, meetings/…/2024-2025/…). Obsidian resolves links
            # by basename across the whole vault, so a non-recursive scan misses
            # subfolder notes and falsely counts every link to them as broken.
            # Matches ChromaDB's recursive index — note that since v0.12 the
            # comparable figure is get_stats()['indexed_notes'] (distinct notes),
            # not ['indexed_rows'], which counts one row per chunk.
            for f in fp.rglob("*.md"):
                try:
                    content = f.read_text(encoding="utf-8")
                except Exception:
                    continue
                # strip only ".md"; dots in the name are part of the note identity.
                # .strip() trailing whitespace so resolution matches _countable_wikilinks,
                # which strips link targets (a few ingested files have trailing-space names).
                note_stem = f.name[:-3].strip()
                resolvable.update(link_names_for_stem(note_stem))
                resolvable.update(a.lower() for a in frontmatter_aliases(content))
                notes.append({"stem": note_stem, "folder": folder, "content": content,
                              "rel": str(f.relative_to(self.cfg.vault))})

        # Obsidian resolves a wikilink against every note in the vault, not just
        # the folders delegation-core manages. Notes do live outside them —
        # MEMORY.md and Vault_Master_Index.md at the root, anything staged in
        # _processed/ — and links to those were being reported broken when they
        # open fine in Obsidian. Widen resolution only; grading above stays scoped
        # to vault_folders, which is what this vault actually curates.
        for f in self.cfg.vault.rglob("*.md"):
            if any(part.startswith(".") for part in f.relative_to(self.cfg.vault).parts):
                continue
            resolvable.update(link_names_for_stem(f.name[:-3]))

        # Folder names (and the vault's own name) used as markers, lowercased.
        folder_markers = {f.lower() for f in self.cfg.vault_folders}
        folder_markers.add(self.cfg.vault.name.lower())

        # Links para arquivo ingerido de fora do vault nao sao quebrados:
        # apontam para algo que search_vault(scope='external') acha. Saem em
        # balde proprio, como os marcadores de pasta, para a contagem dizer
        # de que tipo e cada link em vez de esconder a diferenca.
        ingested = ingested_link_stems()
        total = needs_repair = truncated = orphans = broken_links = 0
        malformed_frontmatter = 0
        malformed_notes: list[dict] = []
        # Collected alongside the counts, not by a second pass: the whole point
        # is that the detail and the summary cannot disagree.
        broken: list[dict] = []
        markers: list[dict] = []
        ingested_links: list[dict] = []
        orphan_notes: list[dict] = []
        repair_notes: list[dict] = []
        truncated_notes: list[dict] = []
        linked_to: set[str] = set()       # stems that are the target of a resolvable link

        # ── Pass 2: grade quality + resolve only note-like, non-code wikilinks ──
        for n in notes:
            total += 1
            content = n["content"]
            # Validated with a real YAML parser, because the one this module
            # uses for everything else CANNOT FAIL: `_parse_frontmatter` splits
            # each line on the first colon and keeps going, so a block that no
            # YAML reader can open is graded as a healthy note.
            #
            # Measured on this vault on 2026-09-03: two transcripts whose title
            # carried an unquoted "Caveat:" made the whole block unreadable —
            # title, date and `type: session-transcript` invisible to Obsidian
            # and to every other YAML consumer — while health reported
            # needs_repair 0, truncated 0, broken_links 0. The hook that wrote
            # them has since been fixed to quote the title; nothing ever looked
            # at the notes it had already written.
            #
            # Cost measured before adding it here: 1.013s over 8.596 blocks,
            # 0.118 ms a note, in a pass that already reads all 8.596 files and
            # is cached for five minutes. A cheaper heuristic (an unquoted
            # value containing ": ") would catch those two and miss an
            # unbalanced bracket — a check that cannot see what it claims to.
            if not _frontmatter_parses(content):
                malformed_frontmatter += 1
                malformed_notes.append({"stem": n["stem"], "folder": n["folder"]})
            try:
                fm = self._parse_frontmatter(content)
                nr = fm.get("needs_review", "").lower() == "true"
                q_str = fm.get("quality_score")
                try:
                    q = float(q_str) if q_str is not None else None
                except (ValueError, TypeError):
                    logger.debug("Unparseable quality_score %r in %s — treating as needs_repair", q_str, n["stem"])
                    q = 0.0
                if nr or (q is not None and q < threshold):
                    needs_repair += 1
                    repair_notes.append({"stem": n["stem"], "folder": n["folder"],
                                         "reason": "needs_review" if nr else f"quality {q}"})
                if fm.get("truncated", "").lower() == "true":
                    truncated += 1
                    truncated_notes.append({"stem": n["stem"], "folder": n["folder"]})
                # Verbatim machine records — graph_build's wiki articles and the
                # SessionEnd hook's raw transcripts — are a different kind of
                # object from a curated note, and both distort these metrics.
                #
                # Deliberately NOT classify_path(): that answers "which graph
                # does this belong to", for search scoping, and knows nothing
                # about session transcripts. This answers "is this a verbatim
                # machine record", which is a wider question. Two definitions
                # because there are two questions — not an oversight to merge.
                n["generated"] = (fm.get("source") == "graph_build"
                                  or fm.get("type") == "session-transcript")

                # Orphans: articles cross-link each other with relative markdown
                # links rather than [[wikilinks]], so the pass below saw every one
                # as unreferenced — one mid-sized repo took orphans from 63 to 662.
                #
                # Broken links: both kinds quote text verbatim, so a `[[...]]`
                # inside is a sample and not a link anyone meant to follow. The
                # graphify report contributed a phantom `[[Foo alloc]]`; a
                # transcript of a conversation about linker code contributed
                # `[[stem]]`, `[[target]]`, `[[source_stem]]`, `[[new-note-stem]]`.
                # Neither kind emits real wikilinks, so nothing is lost by
                # skipping them here.
                if n["generated"]:
                    continue

                for link in _countable_wikilinks(content):
                    key = link.lower()
                    if key in resolvable:
                        linked_to.add(key)
                    elif key in folder_markers:
                        # This vault ends notes with `[[reference]] #digest #pdf`
                        # — a categorisation marker naming a folder, not a link
                        # to a note. 12 of the 26 "broken links" were these, and
                        # no note will ever exist to satisfy them.
                        markers.append({"source": n["stem"], "target": link})
                    elif key in ingested:
                        ingested_links.append({"source": n["stem"],
                                               "folder": n["folder"],
                                               "target": link})
                    else:
                        broken_links += 1
                        broken.append({"source": n["stem"], "folder": n["folder"],
                                       "target": link})
            except Exception:
                pass

        # Orphan = hand-written note nothing links to (a true graph orphan);
        # sessions and generated artifacts excluded.
        generated = 0
        for n in notes:
            if n.get("generated"):
                generated += 1
                continue
            if n["folder"].lower() in skip_orphan:
                continue
            if n["stem"].lower() not in linked_to:
                orphans += 1
                orphan_notes.append({"stem": n["stem"], "folder": n["folder"]})

        unindexed_notes = self._unindexed_notes(notes)

        result = {
            "total_notes": total,
            "needs_repair": needs_repair,
            "truncated": truncated,
            "orphans": orphans,
            "generated_notes": generated,
            "broken_links": broken_links,
            "unindexed": len(unindexed_notes),
            "malformed_frontmatter": malformed_frontmatter,
            "computed_at": datetime.now().isoformat(),
        }
        self._last_health_detail = {
            "_vault": str(self.cfg.vault),
            **result,
            "broken_link_items": broken,
            "orphan_items": orphan_notes,
            "needs_repair_items": repair_notes,
            "truncated_items": truncated_notes,
            "malformed_frontmatter_items": malformed_notes,
            "folder_marker_items": markers,
            "ingested_link_items": ingested_links,
            "unindexed_items": unindexed_notes,
        }
        try:
            cache_path.write_text(
                json.dumps({**result, "computed_at_ts": now,
                        "vault_path": str(self.cfg.vault)}, indent=2), encoding="utf-8"
            )
        except Exception:
            pass
        return result

    def health_detail(self, limit: int = 50) -> dict:
        """The findings behind get_health_summary()'s counts, itemised.

        Exists because the summary returns numbers and nothing else, so acting
        on "broken_links: 26" meant writing a throwaway script to enumerate
        them — and a throwaway script re-implements the definitions. Three such
        scripts over two days reported 248, 63 and 5 against true values of 31,
        31 and 0: one used a bare regex instead of `_countable_wikilinks`, one
        built a narrower resolvable set than the health pass uses, one compared
        an unstripped stem against a stripped link target.

        The lists are collected during the same pass that produces the counts,
        not by a second traversal, so the detail cannot drift from the summary.
        `folder_marker_items` are reported separately: they are `[[reference]]`
        style categorisation markers, deliberately not counted as broken, and
        showing them is what stops the next reader from "fixing" them.

        Always recomputed — the summary's five-minute cache holds counts only.
        """
        # Unconditionally, as the docstring above has always promised. Reusing a
        # populated _last_health_detail made this method answer from the first
        # call for the entire life of the process: fix a broken link, ask again,
        # and the same stale list comes back — which reads as "the fix did not
        # work" and sends the reader off to re-fix something already correct.
        # The summary's disk cache does not help either, since a cache hit
        # returns before _last_health_detail is ever refreshed. This is the
        # diagnostic path, not a hot one; it is called to get the truth.
        self._force_health_recompute()
        detail = self._last_health_detail
        capped = {"limit": limit}
        # Derived from what the health pass actually produced, not from a
        # hardcoded list of keys. The list was the brittle kind: adding
        # `unindexed_items` to the pass and forgetting to add it here would
        # compute the finding correctly and then drop it on the floor, with
        # nothing failing. Any `*_items` list is capped and counted; every other
        # scalar is copied through. `_vault` stays out — it is the pass's own
        # bookkeeping, not a finding.
        for key, value in detail.items():
            if key == "_vault":
                continue
            if key.endswith("_items") and isinstance(value, list):
                capped[key] = value[:limit]
                capped[f"{key}_total"] = len(value)
            else:
                capped[key] = value
        return capped

    def _force_health_recompute(self) -> None:
        """Run the health pass ignoring the summary cache, to populate detail."""
        from .config import vault_health_cache
        cache = vault_health_cache()
        backup = None
        try:
            if cache.exists():
                backup = cache.read_text(encoding="utf-8")
                cache.unlink()
            self.get_health_summary()
        finally:
            if backup is not None and not cache.exists():
                try:
                    cache.write_text(backup, encoding="utf-8")
                except OSError:
                    pass

    def get_notes_needing_repair(self, threshold: float | None = None) -> list[dict]:
        """Return [{path, content, quality_score}] sorted by score ascending (worst first).

        Excludes never_merge_folders (sessions by default) since those are chronological
        records, not synthesis artifacts, and should not be re-synthesized.
        """
        th = threshold if threshold is not None else getattr(self.cfg, "quality_threshold", 0.50)
        # Lowercased on both sides: a vault whose folder is "Sessions" would
        # otherwise match nothing here and re-synthesize chronological records
        # the docstring above promises to leave alone.
        never_merge = {f.lower() for f in
                       getattr(self.cfg, "never_merge_folders", ["sessions"])}
        results = []

        for folder in self.cfg.vault_folders:
            if folder.lower() in never_merge:
                continue
            fp = self.cfg.vault / folder
            if not fp.exists():
                continue
            # rglob, not glob: same subfolder-visibility fix as reindex_vault/
            # list_notes/get_health_summary — otherwise a note nested one folder
            # down never surfaces for healing even when its quality_score is low.
            for f in fp.rglob("*.md"):
                try:
                    content = f.read_text(encoding="utf-8")
                    fm = self._parse_frontmatter(content)
                    nr = fm.get("needs_review", "").lower() == "true"
                    q_str = fm.get("quality_score")
                    try:
                        q = float(q_str) if q_str is not None else None
                    except (ValueError, TypeError):
                        logger.debug("Unparseable quality_score %r in %s — treating as needs_repair", q_str, f.name)
                        q = 0.0
                    if nr or (q is not None and q < th):
                        results.append({
                            "path": str(f.relative_to(self.cfg.vault)),
                            "content": content,
                            "quality_score": q if q is not None else 0.0,
                        })
                except Exception as e:
                    logger.debug("Skipped %s in repair scan: %s", f.name, e)

        results.sort(key=lambda x: x["quality_score"])
        return results

    # ── stats ─────────────────────────────────────────────────────────────────

    @reads_index
    def _client_counts(self) -> dict:
        """{client slug: distinct documents} over the whole index.

        Documents, not rows, for the same reason indexed_notes is: a chunked
        160-page deck would otherwise make one client look like a hundred.
        """
        if not self.collection:
            return {}
        try:
            got = self._paged_get(include=["metadatas"])
        except Exception as e:
            logger.warning("Client count failed: %s", e)
            return {}
        seen: dict[str, set] = {}
        for i, meta in zip(got.get("ids") or [], got.get("metadatas") or []):
            slug = (meta or {}).get("client")
            if not slug:
                continue
            seen.setdefault(slug, set()).add(_CHUNK_SUFFIX_RE.sub("", i))
        return dict(sorted(((k, len(v)) for k, v in seen.items()),
                           key=lambda kv: -kv[1]))

    def get_stats(self) -> dict:
        self._ensure_ready()
        folder_counts = {}
        for folder in self.cfg.vault_folders:
            p = self.cfg.vault / folder
            # rglob, not glob: indexed_notes below counts recursively (reindex_vault
            # uses rglob), so a non-recursive count here made folder_counts
            # undercount vs indexed_notes for any folder with subfolder notes.
            folder_counts[folder] = len(list(p.rglob("*.md"))) if p.exists() else 0
        rows, docs = self._index_counts()
        return {
            # Documents, not rows. collection.count() answered this until v0.12
            # and then started overstating it by ~39% on this vault the moment
            # notes were chunked (3,661 notes → ~5,098 rows), with heartbeat(),
            # vault_stats() and the dashboard header all repeating the inflated
            # number as "notes indexed". Both are reported now rather than one
            # being quietly redefined.
            "indexed_notes": docs,
            "indexed_rows": rows,
            "vault_markdown_files": sum(
                1 for path in self.cfg.vault.rglob("*.md")
                if self.cfg.chroma_path not in path.parents
            ),
            # The clients actually present in the index, as the filter sees
            # them. This is the verification surface for client_path_roots: a
            # derivation rule that mislabels shows up here as a bucket nobody
            # recognises, before anyone relies on the filter. It is also how a
            # caller learns which names are valid — guessing at a normalised
            # slug and getting nothing back is indistinguishable from a client
            # with no documents.
            "clients": self._client_counts(),
            "vault_path": str(self.cfg.vault),
            "embed_model": self.cfg.bge_model,
            "folder_counts": folder_counts,
        }

    @reads_index
    def _index_counts(self) -> tuple[int, int]:
        """(rows, distinct documents) in the collection.

        Distinct documents are counted by discarding every ``::chunk_N`` id with
        N > 0: each document keeps exactly one row this does not match — its
        ::chunk_0, or, for a row written before chunking existed, its bare path.
        An ids-only get() is what makes that affordable; measured on 5k rows it
        costs ~8 ms against ~40 ms to pull the metadatas, and this runs on the
        dashboard's health poll.
        """
        if not self.collection:
            return 0, 0
        rows = self.collection.count()
        try:
            ids = self._paged_get(include=[]).get("ids") or []
        except Exception as e:
            logger.warning("Could not count distinct indexed notes: %s", e)
            return rows, rows
        return rows, sum(1 for i in ids if not _EXTRA_CHUNK_SUFFIX_RE.search(i))
