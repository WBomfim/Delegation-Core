"""
ingest.py : External folder ingestion (field deployment C).

Index files from any path without moving or modifying them.
Uses embeddings.chunk_text for long documents and persists an ingestion registry
so re-runs are safe (upsert semantics, no duplicates).

New in v0.2.
"""

import fnmatch
import json
import logging
import os
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

from .config import CONFIG_DIR
from .embeddings import chunk_text, effective_chunk_chars
from .notes import client_from_path

logger = logging.getLogger("ingest")

_REGISTRY_FILE = CONFIG_DIR / "ingested_sources.json"

#: Extensions that mean "this was a codebase, not a document folder": used only
#: to phrase the hint, not to decide what gets indexed.
_CODE_HINT_EXTS = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".kt", ".scala",
    ".lua", ".zig", ".ex", ".exs", ".dart", ".vue", ".svelte", ".sh", ".sql",
})


def _load_registry() -> dict:
    """O registro de ingestao, ou dicionario vazio quando o arquivo nao serve.

    A checagem de TIPO existe porque tratar so "nao parseia" deixa passar o caso
    pior: um JSON valido do tipo errado. MEDIDO antes desta guarda, com todo
    chamador fazendo `registry[caminho] = ...`:

        ["uma","lista"]  -> list  -> TypeError: list indices must be integers
        "uma string"     -> str   -> TypeError: 'str' object does not support
        42               -> int   -> TypeError: 'int' object does not support

    E o TERCEIRO armazem JSON deste projeto a receber a mesma guarda hoje:
    `jobs.py` e `localqueue.py` ja a tinham, `tracker.py` ganhou de manha e este
    ficou de fora da contagem. O `ingested_sources.json` desta maquina tem
    645 KB e 23 caminhos indexados.

    Descarta em voz alta: perder 23 caminhos em silencio faz o proximo
    `ingest_folder` reindexar tudo sem ninguem entender por que.
    """
    if not _REGISTRY_FILE.exists():
        return {}
    try:
        dados = json.loads(_REGISTRY_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("ingest registry unreadable (%s) - starting empty", e)
        return {}
    if not isinstance(dados, dict):
        logger.warning(
            "ingest registry at %s holds %s, not a dict - ignoring it. The file "
            "was not deleted; move it aside if the content matters.",
            _REGISTRY_FILE, type(dados).__name__)
        return {}
    return dados


def _atomic_write_registry(registry: dict):
    target_dir = _REGISTRY_FILE.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=str(target_dir), delete=False, encoding="utf-8") as tf:
            json.dump(registry, tf, indent=2)
            tmp = tf.name
        os.replace(tmp, str(_REGISTRY_FILE))
    except Exception as e:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        logger.warning("Could not save ingest registry: %s", e)


def _save_registry(registry: dict):
    _atomic_write_registry(registry)


def _update_source_entry(source_key: str, entry: dict):
    try:
        fresh = _load_registry()
        fresh[source_key] = entry
        _atomic_write_registry(fresh)
    except Exception as e:
        logger.warning("Could not update registry entry for %s: %s", source_key, e)


def _remove_source_entry(source_key: str) -> bool:
    try:
        fresh = _load_registry()
        had_entry = fresh.pop(source_key, None) is not None
        _atomic_write_registry(fresh)
        return had_entry
    except Exception as e:
        logger.warning("Could not remove registry entry for %s: %s", source_key, e)
        return False


def fontes_do_indice(chroma_dir: Path) -> dict[str, dict[str, str]]:
    """As fontes ingeridas que um indice guarda: {fonte: {arquivo: ingested_at}}.

    Lido do `chroma.sqlite3` com o sqlite da biblioteca padrao, em modo so
    leitura, e nao pelo chromadb. E o que permite ler um indice que trava ou
    derruba quem o abre pelo chromadb (o defeito vive no HNSW e no runtime em
    Rust, nao nas tabelas), e ler o indice vivo sem ser um segundo escritor
    ao lado do daemon.

    Cada linha externa carrega `source_folder`, `path` e `ingested_at`
    (ver `ingest`), entao o indice sabe tudo o que o registro sabia, menos
    `recursive` e `exclude`.
    """
    import sqlite3

    banco = Path(chroma_dir) / "chroma.sqlite3"
    if not banco.exists():
        return {}
    conexao = sqlite3.connect(f"file:{banco}?mode=ro", uri=True, timeout=30)
    try:
        linhas = conexao.execute(
            "SELECT id, key, string_value FROM embedding_metadata "
            "WHERE key IN ('source_folder', 'path', 'ingested_at', 'is_external')"
        ).fetchall()
    finally:
        conexao.close()

    por_linha: dict[int, dict[str, str]] = {}
    for linha_id, chave, valor in linhas:
        por_linha.setdefault(linha_id, {})[chave] = valor
    fontes: dict[str, dict[str, str]] = {}
    for meta in por_linha.values():
        if str(meta.get("is_external", "")).lower() != "true":
            continue
        fonte, arquivo = meta.get("source_folder"), meta.get("path")
        if not fonte or not arquivo:
            continue
        arquivos = fontes.setdefault(fonte, {})
        # Um arquivo em chunks tem uma linha por chunk, todas com o mesmo
        # ingested_at; o maior cobre o caso de reingestao parcial.
        arquivos[arquivo] = max(arquivos.get(arquivo, ""), meta.get("ingested_at") or "")
    return fontes


def reconstruir_registro_do_indice(chroma_dir: Path, carimbar_arquivos: bool) -> dict:
    """Devolve as fontes do indice em `chroma_dir` somadas ao registro atual.

    Existe porque em 29/09/2026 um teste gravou por cima do registro real em
    duas maquinas (ver tests/test_guarda_de_estado.py), e o registro e a unica
    coisa que diz quais fontes reingerir. O indice diz a mesma coisa, e estava
    intacto: a reconstrucao le a lista dele, sem restaurar backup nenhum.

    Entradas que o registro ja tem ficam como estao: e nelas que moram
    `recursive` e `exclude`, que o indice nao guarda. Fonte que so o indice
    conhece entra com `recursive: True`, o padrao de `ingest`.

    `carimbar_arquivos`: True quando `chroma_dir` e o indice que vai continuar
    em uso. Cada arquivo presente que nao mudou desde o `ingested_at` recebe o
    carimbo de mtime e tamanho, e o proximo `ingest` o pula; um arquivo que
    mudou fica sem carimbo e e reembutido. False quando o indice esta em
    quarentena: as linhas dele nao existem no indice novo, entao nenhum
    carimbo pode dizer que existem.
    """
    registro = _load_registry()
    fontes = fontes_do_indice(chroma_dir)
    novas = 0
    for fonte, arquivos in fontes.items():
        entrada = registro.get(fonte)
        if not isinstance(entrada, dict):
            entrada = {"recursive": True, "exclude": None, "files": {},
                       "reconstruido_do_indice": True}
            registro[fonte] = entrada
            novas += 1
        if not carimbar_arquivos:
            continue
        carimbos = entrada.setdefault("files", {})
        for arquivo, ingerido_em in arquivos.items():
            if arquivo in carimbos:
                continue
            try:
                st = Path(arquivo).stat()
                momento = datetime.fromisoformat(ingerido_em).timestamp()
            except (OSError, ValueError):
                continue
            if st.st_mtime <= momento:
                carimbos[arquivo] = [st.st_mtime, st.st_size]
    logger.info("Registro de ingestao reconstruido de %s: %d fontes no indice, %d novas",
                chroma_dir, len(fontes), novas)
    return registro


def _paged_get(collection, limit: int = 5000, **kwargs) -> dict:
    """Safely get rows from ChromaDB in batches to prevent SQLite 'too many SQL variables'."""
    offset = 0
    all_ids = []
    all_metas = []
    try:
        while True:
            chunk = collection.get(limit=limit, offset=offset, **kwargs)
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
        # Fallback for test mocks or collection wrappers that don't support limit/offset
        return collection.get(**kwargs)


def is_excluded(path: Path, source: Path, patterns: list[str]) -> bool:
    """Whether a glob pattern from `exclude` keeps this file out.

    Matched against three shapes, because callers reach for all three and a
    pattern that quietly matches nothing looks exactly like a clean folder:
    the path relative to source (`Logs/*`), the file name (`*.log`), and any
    single path component (`Logs`).
    """
    if not patterns:
        return False
    try:
        rel = path.relative_to(source).as_posix()
    except ValueError:
        rel = path.name
    parts = rel.split("/")
    for p in patterns:
        if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(path.name, p):
            return True
        if any(fnmatch.fnmatch(part, p) for part in parts):
            return True
    return False


def _configured_sources(cfg) -> list[dict]:
    """Return only well-formed source entries from the user configuration."""
    raw = getattr(cfg, "ingest_sources", [])
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict) and str(entry.get("path", "")).strip()]


def _has_source_policy(cfg) -> bool:
    """A non-empty configured list is an allow-list even if an entry is malformed."""
    raw = getattr(cfg, "ingest_sources", [])
    return isinstance(raw, list) and bool(raw)


def _merge_patterns(*groups) -> list[str]:
    """Combine configured and per-run patterns without changing their order."""
    merged = []
    for group in groups:
        if not isinstance(group, list):
            continue
        for pattern in group:
            if isinstance(pattern, str) and pattern.strip() and pattern not in merged:
                merged.append(pattern)
    return merged


class IngestManager:
    """Index external files into the vault's ChromaDB without touching them on disk.

    External results are tagged folder='_external' so search_vault can distinguish
    them from vault notes. Each file's absolute path is the ChromaDB document ID,
    so re-indexing the same path is safe.
    """

    def __init__(self, vault_manager):
        self._vault = vault_manager
        self._cfg = vault_manager.cfg

    def _configured_source_for(self, source: Path) -> dict | None:
        """Find the configured source that exactly authorizes ``source``."""
        for entry in _configured_sources(self._cfg):
            try:
                configured_path = Path(str(entry["path"])).expanduser().resolve()
            except (OSError, ValueError):
                continue
            if configured_path == source:
                return entry
        return None

    def ingest(self, source_path: str, recursive: bool = True, force: bool = False,
               exclude: list[str] | None = None) -> dict:
        """Index all supported files under source_path.

        source_path: absolute path to a file or directory.
        recursive: walk subdirectories (default True).
        force: re-index even if file mtime and size are unchanged (default False).
        exclude: glob patterns for paths to leave out (default none).

        On exclude: without it the only control over what gets indexed is which
        directory you point at, so a folder holding one useful document and a
        build log costs you the log in the index. Measured on a real vault: a
        `Logs/` subdirectory of MD5 manifests and file listings, 188 thousand
        lines of hashes and paths, took 19.5 minutes to embed and answered no
        question anybody would ask. The caller could only avoid it by ingesting
        each useful subfolder separately, which is a workaround, not a control.
        """
        from .extractor import DatalessFileError, SUPPORTED, UnreadableFileError, extract
        from .imagens import EXTENSOES as _IMAGENS, markdown_tem_texto as _tem_texto

        source = Path(source_path).expanduser().resolve()
        if not source.exists():
            return {"error": f"Path not found: {source_path}"}

        configured = _has_source_policy(self._cfg)
        configured_source = self._configured_source_for(source)
        if configured and configured_source is None:
            return {
                "error": (
                    f"Source is not configured: {source}. Add it to ingest_sources "
                    "before ingesting it."
                )
            }
        if configured_source is not None and not configured_source.get("enabled", True):
            return {"error": f"Configured source is disabled: {configured_source.get('name') or source}"}

        candidates: list[Path]
        unsupported: Counter[str] = Counter()

        patterns = _merge_patterns(
            getattr(self._cfg, "ingest_exclude_patterns", []),
            configured_source.get("exclude", []) if configured_source else [],
            exclude or [],
        )
        excluded: list[str] = []

        def _keep(f: Path) -> bool:
            if f.name.startswith("~$"):
                return False
            if is_excluded(f, source, patterns):
                excluded.append(f.name)
                return False
            if f.suffix.lower() in SUPPORTED:
                return True
            unsupported[f.suffix.lower() or "(sem extensão)"] += 1
            return False

        if source.is_file():
            candidates = [source] if _keep(source) else []
        else:
            candidates = []
            if recursive:
                # Prune excluded directories before visiting their children. A
                # post-filter still has to enumerate every path in
                # node_modules or a virtualenv, which defeats the operational
                # point of configuring those directories in the first place.
                for root, directories, filenames in os.walk(source):
                    root_path = Path(root)
                    kept_directories = []
                    for directory in directories:
                        candidate_dir = root_path / directory
                        if is_excluded(candidate_dir, source, patterns):
                            excluded.append(candidate_dir.relative_to(source).as_posix())
                        else:
                            kept_directories.append(directory)
                    directories[:] = kept_directories
                    for filename in filenames:
                        candidate = root_path / filename
                        if _keep(candidate):
                            candidates.append(candidate)
            else:
                candidates = [f for f in source.iterdir() if f.is_file() and _keep(f)]

        indexed: list[str] = []
        skipped_empty: list[str] = []
        skipped_unreadable: list[str] = []
        skipped_dataless: list[str] = []
        skipped_unchanged: list[str] = []
        errors: list[str] = []
        now = datetime.now().isoformat()

        max_chars = effective_chunk_chars(
            self._cfg.bge_model,
            self._cfg.ingest_chunk_size,
            self._cfg.embed_max_seq_length,
        )
        overlap = self._cfg.ingest_chunk_overlap

        registry = _load_registry()
        source_key = str(source)
        source_meta = registry.get(source_key, {})
        cached_files = source_meta.get("files", {})
        new_cached_files = {}

        for f in candidates:
            f_str = str(f)
            try:
                st = f.stat()
                f_mtime = st.st_mtime
                f_size = st.st_size
            except OSError as e:
                logger.warning("Stat failed for %s: %s", f.name, e)
                skipped_unreadable.append(f.name)
                errors.append(f"{f.name}: {e}")
                continue

            # Incremental skip: check if file has already been indexed and is unmodified
            if not force and f_str in cached_files:
                c_info = cached_files[f_str]
                if isinstance(c_info, (list, tuple)) and len(c_info) >= 2:
                    if c_info[0] == f_mtime and c_info[1] == f_size:
                        # Counted as unchanged, NOT as indexed. Reporting a skip
                        # as an index made "N arquivos reingeridos" indistinguishable
                        # from "N arquivos already present": the number stayed
                        # plausible on a run that embedded nothing at all, which is
                        # the one case where the operator needs to know.
                        skipped_unchanged.append(f_str)
                        new_cached_files[f_str] = [f_mtime, f_size]
                        continue

            try:
                content = extract(f)
                if not content or not content.strip():
                    skipped_empty.append(f.name)
                    continue
                # Imagem sem texto lido (icone, foto sem legenda, OCR ausente)
                # vira so metadado: no _inbox isso ainda serve, porque alguem pos
                # a imagem la; numa pasta ingerida seria linha sem conteudo.
                if f.suffix.lower() in _IMAGENS and not _tem_texto(content):
                    skipped_empty.append(f.name)
                    continue

                chunks = chunk_text(content, max_chars=max_chars, overlap=overlap)
                # Ingested files are the bulk of a real index (92.8% of rows on
                # the deployment that asked for client scoping), so a filter that
                # skipped them would reach a fourteenth of the corpus and read as
                # "that client has almost nothing". Derived from the configured
                # roots only; no root, no client, never a guess.
                client = client_from_path(
                    f_str,
                    getattr(self._cfg, "client_path_roots", None),
                    getattr(self._cfg, "client_aliases", None),
                )
                for i, chunk in enumerate(chunks):
                    chunk_id = f"{f}::chunk_{i}" if len(chunks) > 1 else str(f)
                    meta = {
                        "title":         f.stem,
                        "path":          f_str,
                        "folder":        "_external",
                        "source_folder": source_key,
                        "ingested_at":   now,
                        "is_external":   "true",
                        "chunk":         str(i),
                        "total_chunks":  str(len(chunks)),
                    }
                    if client:
                        meta["client"] = client
                    self._vault.index_note(chunk, meta, doc_id=chunk_id)
                indexed.append(f_str)
                new_cached_files[f_str] = [f_mtime, f_size]
            except DatalessFileError as e:
                logger.warning("Dataless file %s: %s", f.name, e)
                skipped_dataless.append(f.name)
                errors.append(f"{f.name}: {e}")
            except UnreadableFileError as e:
                logger.warning("Unreadable file %s: %s", f.name, e)
                skipped_unreadable.append(f.name)
                errors.append(f"{f.name}: {e}")
            except Exception as e:
                logger.warning("Ingest error %s: %s", f.name, e)
                skipped_unreadable.append(f.name)
                errors.append(f"{f.name}: {e}")

        merged_files = dict(cached_files) if (not force and not source.is_file()) else {}
        merged_files.update(new_cached_files)

        source_entry = {
            "last_indexed":           now,
            "indexed_count":          len(indexed),
            "skipped_unchanged_count": len(skipped_unchanged),
            "skipped_empty_count":     len(skipped_empty),
            "skipped_unreadable_count": len(skipped_unreadable),
            "skipped_dataless_count":  len(skipped_dataless),
            "error_count":            len(errors),
            "recursive":              recursive,
            "exclude":                patterns if patterns else None,
            "files":                  merged_files,
        }
        _update_source_entry(source_key, source_entry)

        total_skipped = len(skipped_empty) + len(skipped_unreadable) + len(skipped_dataless)
        result = {
            "source":             source_key,
            "indexed":            len(indexed),
            "skipped":            total_skipped,
            "skipped_empty":      len(skipped_empty),
            "skipped_unreadable": len(skipped_unreadable),
            "skipped_dataless":   len(skipped_dataless),
            "skipped_unchanged":  len(skipped_unchanged),
            "errors":             errors,
        }
        if configured_source is not None:
            result["configured_source"] = configured_source.get("name") or source_key
        if excluded:
            # Reported, not silent: a pattern that matches more than the caller
            # meant looks exactly like a folder with fewer files in it.
            result["excluded"] = len(excluded)
            result["excluded_files"] = excluded[:20]
        if skipped_dataless:
            result["dataless_files"] = skipped_dataless
        if unsupported:
            result["unsupported"] = dict(unsupported.most_common(12))
            result["unsupported_total"] = sum(unsupported.values())
            code_like = sum(n for ext, n in unsupported.items() if ext in _CODE_HINT_EXTS)
            if code_like:
                result["hint"] = (
                    f"{code_like} code file(s) were not indexed: ingest_folder handles "
                    "documents only. Use graph_build() to make a codebase searchable."
                )
        return result

    def ingest_configured(self, name: str = "", force: bool = False) -> dict:
        """Ingest one named configured source, or every enabled source when name is empty."""
        configured = _configured_sources(self._cfg)
        if not configured:
            return {"error": "No ingest_sources are configured."}

        selected = [entry for entry in configured if not name or entry.get("name") == name]
        if name and not selected:
            return {"error": f"Configured source not found: {name}"}

        results = []
        for entry in selected:
            if not entry.get("enabled", True):
                continue
            result = self.ingest(
                str(entry["path"]),
                recursive=bool(entry.get("recursive", True)),
                force=force,
            )
            result["name"] = entry.get("name") or str(entry["path"])
            results.append(result)

        return {
            "sources": results,
            "configured_count": len(results),
            "indexed": sum(result.get("indexed", 0) for result in results),
            "skipped": sum(result.get("skipped", 0) for result in results),
            "errors": [result["error"] for result in results if "error" in result],
        }

    def forget(self, source_path: str) -> dict:
        """Drop everything previously ingested from source_path.

        ingest is upsert-by-absolute-path, which is safe for re-runs but leaves
        rows behind forever once the source moves or is deleted: they keep
        answering searches with paths that no longer resolve. Matches the source
        folder strictly or any file contained beneath it.
        """
        source_p = Path(source_path).expanduser().resolve()
        source_str = str(source_p)
        collection = getattr(self._vault, "collection", None)
        if collection is None:
            self._vault._ensure_ready()
            collection = getattr(self._vault, "collection", None)
        if collection is None:
            return {"error": "Vault not initialized"}

        removed = 0
        try:
            rows = _paged_get(collection, where={"is_external": "true"}, include=["metadatas"])
            ids = []
            for doc_id, meta in zip(rows.get("ids") or [], rows.get("metadatas") or []):
                meta = meta or {}
                if meta.get("source_folder") == source_str:
                    ids.append(doc_id)
                    continue
                p_str = meta.get("path", "")
                if p_str:
                    try:
                        p = Path(p_str)
                        # Exact match or parent directory containment (not loose string startswith)
                        if p == source_p or source_p in p.parents:
                            ids.append(doc_id)
                    except Exception:
                        pass
            if ids:
                # Delete in batches to avoid SQLite variable limits
                batch_size = 5000
                for i in range(0, len(ids), batch_size):
                    collection.delete(ids=ids[i:i + batch_size])
                removed = len(ids)
        except Exception as e:
            logger.warning("Ingest forget failed for %s: %s", source_str, e)
            return {"error": str(e)}

        had_entry = _remove_source_entry(source_str)
        return {"source": source_str, "removed_chunks": removed, "registry_entry_removed": had_entry}


    def status(self) -> dict:
        """Return the ingestion registry: which paths have been indexed, file counts and existence."""
        registry = _load_registry()
        sources_status = {}
        missing_sources = []
        for src, info in registry.items():
            exists = Path(src).exists()
            entry = dict(info) if isinstance(info, dict) else {"last_indexed": str(info)}
            entry["exists"] = exists
            if not exists:
                missing_sources.append(src)
            sources_status[src] = entry
        configured_status = []
        for entry in _configured_sources(self._cfg):
            raw_path = str(entry["path"])
            try:
                path = Path(raw_path).expanduser().resolve()
                exists = path.exists()
                path_text = str(path)
            except (OSError, ValueError):
                exists = False
                path_text = raw_path
            configured_status.append({
                "name": entry.get("name") or path_text,
                "path": path_text,
                "recursive": bool(entry.get("recursive", True)),
                "enabled": bool(entry.get("enabled", True)),
                "exists": exists,
            })

        res = {
            "sources": sources_status,
            "count": len(registry),
            "configured_sources": configured_status,
            "configured_source_count": len(configured_status),
        }
        if missing_sources:
            res["missing_sources"] = missing_sources
            res["hint"] = "Some ingested source folders no longer exist on disk. Run ingest_forget(<source>) to clean them."
        return res
