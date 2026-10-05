"""
doctor.py: Installation drift and vault hygiene checks.

Every check here exists because the condition it looks for went unnoticed on a
live machine, sometimes for weeks:

* The SessionEnd hook installed in ~/.delegation_core/hooks/ had fallen behind
  the repo copy, so a fix shipped in the repo was simply not running.
* That stale hook wrote transcripts to a hardcoded lowercase ``sessions/`` beside
  the vault's configured ``Sessions/``. Indexing, search and health accounting
  all iterate ``vault_folders``, so 29 transcripts spanning seven weeks were
  invisible: no error anywhere, just absence.
* The [graph] extra was never installed in the live venv, so graph_build failed
  on a missing import rather than on anything to do with graphs.
* ``ingest_folder`` registry entries outlive the folders they point at, leaving
  rows that answer searches with paths that no longer resolve.
* Every ``scope``-filtered search died on "Error finding id" while unfiltered
  search kept answering, so the whole hand-written slice of the vault was
  unreachable and nothing said so. doctor passed 6/6 green throughout, because
  nothing here had ever asked the index a question.

None of these surface through normal use. Each returns a status of ok | warn |
error plus a one-line fix.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

CONFIG_DIR = Path.home() / ".delegation_core"

#: Folders delegation-core creates itself; a case-variant sibling of a
#: configured folder is a bug, but these are not.
_VAULT_INTERNAL = {"_inbox", "_processed", "_failed", "_archive"}


def check_hooks(settings_path: Path | None = None) -> dict:
    """Os hooks de sessao estao registrados no Claude Code, e apontam para algo
    que existe?

    Ate a v0.14.0 este check comparava as copias de ~/.delegation_core/hooks/
    com a arvore, porque a copia envelhecia sem aviso. Desde a v0.15.0 nao ha
    copia: o registro aponta para o `delegation-core-hook` do venv. O que pode
    dar errado agora e o registro faltar, apontar para um executavel que sumiu,
    ou ainda chamar um dos scripts copiados de antes.
    """
    import json as _json

    from .clients import CLAUDE_SETTINGS, HOOK_EVENTS, LEGACY_HOOK_SCRIPTS

    path = settings_path or CLAUDE_SETTINGS
    fix = "delegation-core update  (registers the session hooks)"
    try:
        dados = _json.loads(path.read_text(encoding="utf-8") or "{}") if path.exists() else {}
    except (OSError, ValueError) as e:
        return {"check": "hooks", "status": "warn",
                "detail": f"{path} unreadable ({type(e).__name__}): hooks not checked"}
    eventos = dados.get("hooks") if isinstance(dados, dict) else None
    eventos = eventos if isinstance(eventos, dict) else {}

    comandos = {ev: [h.get("command", "") for g in (eventos.get(ev) or []) if isinstance(g, dict)
                     for h in (g.get("hooks") or []) if isinstance(h, dict)]
                for ev in HOOK_EVENTS}
    antigos = sorted({s for cmds in comandos.values() for c in cmds for s in LEGACY_HOOK_SCRIPTS
                      if s in str(c).replace("\\", "/")})
    faltando, quebrados = [], []
    for ev, arg in HOOK_EVENTS.items():
        nossos = [c for c in comandos[ev] if "delegation-core-hook" in c and arg in c]
        if not nossos:
            faltando.append(ev)
            continue
        exe = nossos[0].split('"')[1] if nossos[0].startswith('"') else nossos[0].split()[0]
        if not Path(exe).exists():
            quebrados.append(f"{ev} -> {exe}")

    if antigos:
        return {"check": "hooks", "status": "warn",
                "detail": f"still registered as copied scripts: {antigos}", "fix": fix}
    if faltando or quebrados:
        partes = ([f"not registered: {faltando}"] if faltando else []) + \
                 ([f"executable missing: {quebrados}"] if quebrados else [])
        return {"check": "hooks", "status": "warn", "detail": "; ".join(partes), "fix": fix}
    return {"check": "hooks", "status": "ok",
            "detail": "SessionStart and SessionEnd run delegation-core-hook"}


def check_vault_folders(cfg) -> dict:
    """Configured folders must exist, and no case-variant sibling may shadow one."""
    vault = cfg.vault
    if not vault.is_dir():
        return {"check": "vault_folders", "status": "error",
                "detail": f"vault path does not exist: {vault}",
                "fix": "delegation-core setup"}

    configured = {f: (vault / f) for f in cfg.vault_folders}
    missing = [f for f, p in configured.items() if not p.is_dir()]

    lowered = {f.lower(): f for f in cfg.vault_folders}
    shadows = []
    for child in sorted(p for p in vault.iterdir() if p.is_dir()):
        if child.name.startswith(".") or child.name in _VAULT_INTERNAL:
            continue
        canonical = lowered.get(child.name.lower())
        if canonical and child.name != canonical:
            n = len(list(child.rglob("*.md")))
            shadows.append(f"{child.name}/ shadows {canonical}/ ({n} note(s) invisible to search)")

    if shadows:
        return {"check": "vault_folders", "status": "error",
                "detail": "; ".join(shadows),
                "fix": "move the notes into the configured folder and remove the stray directory"}
    if missing:
        return {"check": "vault_folders", "status": "warn",
                "detail": f"configured but absent: {missing}",
                "fix": "they are created on first write; remove them from vault_folders if unwanted"}
    return {"check": "vault_folders", "status": "ok",
            "detail": f"{len(configured)} folder(s) present, no case-variant shadows"}


def check_graph_extra() -> dict:
    try:
        import tree_sitter  # noqa: F401
        import tree_sitter_python  # noqa: F401
    except Exception as e:
        return {"check": "graph_extra", "status": "warn",
                "detail": f"code-graph pipeline unavailable ({type(e).__name__}: {e})",
                "fix": 'pip install "delegation-core[graph]"'}
    return {"check": "graph_extra", "status": "ok", "detail": "tree-sitter parsers importable"}


def check_engine_mode(cfg) -> dict:
    if cfg.is_agent_mode:
        return {"check": "engine_mode", "status": "ok",
                "detail": "agent: no local model is loaded; generation is delegated"}
    missing = []
    if not Path(cfg.llama_binary).exists():
        missing.append(f"llama_binary {cfg.llama_binary}")
    if not Path(cfg.llama_model).exists():
        missing.append(f"llama_model {cfg.llama_model}")
    if missing:
        return {"check": "engine_mode", "status": "error",
                "detail": f"engine_mode={cfg.engine_mode} but missing: {', '.join(missing)}",
                "fix": 'set engine_mode to "agent", or fix the paths in config.json'}
    return {"check": "engine_mode", "status": "ok",
            "detail": f"{cfg.engine_mode}: binary and model present"}


def check_ingest_registry() -> dict:
    reg_path = CONFIG_DIR / "ingested_sources.json"
    if not reg_path.exists():
        return {"check": "ingest_registry", "status": "ok", "detail": "no external folders ingested"}
    try:
        registry = json.loads(reg_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"check": "ingest_registry", "status": "warn",
                "detail": f"registry unreadable: {e}", "fix": f"delete {reg_path}"}
    gone = [p for p in registry if not Path(p).exists()]
    if gone:
        return {"check": "ingest_registry", "status": "warn",
                "detail": f"{len(gone)} ingested path(s) no longer exist: {gone[:3]}",
                "fix": "ingest_forget(<path>) to drop their rows from the index"}
    return {"check": "ingest_registry", "status": "ok",
            "detail": f"{len(registry)} ingested path(s), all still present"}


def check_graph_registry(cfg) -> dict:
    reg = cfg.graphs_registry_path
    if not reg.exists():
        return {"check": "graph_registry", "status": "ok", "detail": "no graphs built"}
    try:
        registry = json.loads(reg.read_text(encoding="utf-8"))
    except Exception as e:
        return {"check": "graph_registry", "status": "warn",
                "detail": f"registry unreadable: {e}", "fix": f"delete {reg}"}
    gone = [n for n, e in registry.items() if not Path(e.get("source_path", "")).exists()]
    untracked = [n for n, e in registry.items() if e.get("node_count") and "vault_paths" not in e]
    if gone or untracked:
        bits = []
        if gone:
            bits.append(f"source gone: {gone}")
        if untracked:
            bits.append(f"built before vault_paths tracking (a rebuild cannot clean them): {untracked}")
        return {"check": "graph_registry", "status": "warn", "detail": "; ".join(bits),
                "fix": "graph_build(..., force=true) to refile, or delete the stale entry"}
    return {"check": "graph_registry", "status": "ok",
            "detail": f"{len(registry)} graph(s), all sources present and tracked"}


#: The metadata filters search_vault puts on a query, copied from search()'s own
#: branches. A scope that cannot be queried is a scope that answers nothing,
#: however healthy the counts look. is_external is the *string* "true": that is
#: what ingest writes and what search queries; the boolean matches no row and
#: would make this probe pass without testing anything.
_SCOPE_FILTERS = ({"kind": "note"}, {"kind": "generated"}, {"is_external": "true"})


#: Probe body, run in a child process. See check_index_integrity for why.
_PROBE_SOURCE = """
import json, sys, warnings
warnings.filterwarnings("ignore")
import chromadb

path, name, filters = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
client = chromadb.PersistentClient(
    path=path, settings=chromadb.Settings(anonymized_telemetry=False))
# No embedding_function: this must not pull BGE into memory just to probe.
collection = client.get_collection(name=name)
dimension = collection._model.dimension or 0
if not dimension:
    print(json.dumps({"empty": True}))
    raise SystemExit(0)

probe = [1.0] + [0.0] * (dimension - 1)
broken = []
for where in filters:
    try:
        collection.query(query_embeddings=[probe], n_results=1, where=where)
    except Exception as e:
        broken.append(f"{where}: {type(e).__name__}: {e}")
print(json.dumps({"broken": broken, "count": collection.count()}))
"""


def sondar_indice(cfg, timeout: int = 120) -> dict:
    """Roda a sonda num processo filho. {completed, sinal} ou {timeout: True}.

    `sinal` e o numero do sinal que matou o filho, ou None. E a unica leitura
    que `recuperacao` faz daqui: morte por sinal ao abrir e a condicao que ela
    conserta, e nenhuma outra resposta desta sonda justifica uma quarentena.
    """
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _PROBE_SOURCE, str(cfg.chroma_path),
             cfg.collection_name, json.dumps(list(_SCOPE_FILTERS))],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"timeout": True, "sinal": None}
    sinal = -completed.returncode if completed.returncode < 0 else None
    # No Windows nao ha sinal: a violacao de acesso sai como o codigo NTSTATUS
    # 0xC0000005. Sem isto a recuperacao automatica nunca dispararia la.
    if sinal is None and completed.returncode in _NTSTATUS_DE_CRASH:
        sinal = 11
    return {"completed": completed, "sinal": sinal}


#: Codigos de saida do Windows para processo morto por violacao de acesso ou
#: estouro de pilha, sem e com sinal (a mesma palavra lida como int32).
_NTSTATUS_DE_CRASH = frozenset({0xC0000005, 0xC00000FD, -1073741819, -1073741571})


def check_index_location(cfg) -> dict:
    """O indice mora numa pasta que um cliente de nuvem sincroniza?

    Um dos dois Macs com indice danificado em 29/09/2026 tinha o vault, e com ele
    o `.chroma_bge`, dentro do OneDrive. Sincronizacao que copia, trava ou
    substitui arquivos do SQLite e do HNSW por baixo de um processo que os tem
    abertos e causa conhecida de dano, e nada avisava.
    """
    from .config import caminho_local_do_indice, em_pasta_sincronizada
    if not em_pasta_sincronizada(cfg.chroma_path):
        return {"check": "index_location", "status": "ok",
                "detail": f"index at {cfg.chroma_path}, outside any synced folder"}
    return {"check": "index_location", "status": "warn",
            "detail": f"index at {cfg.chroma_path} is inside a cloud-synced folder; "
                      "sync touching the database under an open process damages it",
            "fix": "with the daemon stopped: delegation-core recover-index "
                   f"--index-path {caminho_local_do_indice()} (the index is rebuilt "
                   "there from the vault and the ingest sources)"}


def check_index_integrity(cfg) -> dict:
    """Ask the index the question that breaks, rather than inspecting its files.

    A filtered query is the only authoritative test. Comparing ids between
    chroma.sqlite3 and the vector segment looks rigorous and is not: records live
    in memory until Chroma flushes them, so a healthy server with pending writes
    is indistinguishable from a corrupt index, and ``index_metadata.pickle`` (the
    obvious place to read ids from) is a legacy artifact that current Chroma does
    not create for new collections at all.

    **The probe runs in a child process because it can take the interpreter with
    it.** Opening a PersistentClient and querying it segfaulted the whole CLI
    (SIGSEGV, exit 139) on a live index after a bulk ingest; the coredump showed
    unbounded recursion inside chromadb_rust_bindings. It reproduced on a copy
    with no other process running, so it was the index state rather than
    contention, but the state was never narrowed further. The obvious suspect,
    a deep pending write log, was ruled out: a rebuild passed the same depth and
    opened normally.

    That is unresolved, and it is the point. A diagnostic that dies on a condition
    it cannot explain is worse than no diagnostic, and this one runs right after
    loading data, which is when the condition appeared. In a child process the
    same crash becomes an answer.
    """
    if not (cfg.chroma_path / "chroma.sqlite3").exists():
        return {"check": "index_integrity", "status": "ok", "detail": "no index built yet"}

    sonda = sondar_indice(cfg)
    if sonda.get("timeout"):
        return {"check": "index_integrity", "status": "warn",
                "detail": "index did not answer within 120s",
                "fix": "delegation-core reindex --force"}
    completed = sonda["completed"]

    if sonda["sinal"] is not None:
        # What was measured on the one occurrence, and nothing beyond it: every
        # newly opened client died on this index while the running server kept
        # answering from memory, `reindex --force` died the same way without
        # touching a row, and a fresh path worked normally. The cause was never
        # established: a deep pending write log looked responsible and was not:
        # a rebuild reproduced that depth and opened fine. So describe the
        # symptom and the exit, and claim no more than that.
        return {"check": "index_integrity", "status": "error",
                "detail": f"opening the index crashed the probe (signal "
                          f"{sonda['sinal']}): every new process that opens it "
                          "will crash the same way; a running server keeps working from "
                          "memory",
                "fix": "a server already running keeps answering from memory, so do "
                       "not restart it in a hurry; when ready, stop it and run "
                       "delegation-core recover-index, which moves this index aside so "
                       "the daemon rebuilds it on its next start (a daemon that crashes "
                       "opening it does this by itself). reindex --force crashes on "
                       "this state"}
    if completed.returncode != 0:
        tail = (completed.stderr or "").strip().splitlines()
        return {"check": "index_integrity", "status": "warn",
                "detail": f"index unreadable ({tail[-1] if tail else 'no output'})",
                "fix": "delegation-core reindex --force"}

    try:
        result = json.loads(completed.stdout.strip().splitlines()[-1])
    except Exception:
        return {"check": "index_integrity", "status": "skip",
                "detail": "probe returned nothing parseable"}

    if result.get("empty"):
        return {"check": "index_integrity", "status": "skip",
                "detail": "collection has no vectors yet"}

    if result["broken"]:
        return {"check": "index_integrity", "status": "error",
                "detail": "scope-filtered search fails: " + "; ".join(result["broken"]),
                "fix": "delegation-core reindex --force, then restart the MCP server "
                       "(it holds the old index in memory and will not re-read disk)"}

    return {"check": "index_integrity", "status": "ok",
            "detail": f"{result['count']} row(s), every search scope answers"}


def check_orphan_segments(cfg) -> dict:
    """Detect segment directories left behind on disk when collections were recreated."""
    chroma_dir = cfg.chroma_path
    db_path = chroma_dir / "chroma.sqlite3"

    abandoned_dirs = []
    if cfg.vault and cfg.vault.is_dir():
        abandoned_dirs = [
            p for p in cfg.vault.glob(".chroma*")
            if p.is_dir() and p.resolve() != chroma_dir.resolve()
        ]

    if not db_path.exists():
        if abandoned_dirs:
            abandoned_mb = sum(
                sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                for d in abandoned_dirs
            ) / (1024 * 1024)
            return {
                "check": "orphan_segments",
                "status": "warn",
                "detail": f"{len(abandoned_dirs)} abandoned legacy index directory(ies) ({abandoned_mb:.1f} MB)",
                "fix": "run: delegation-core doctor --clean-orphans",
            }
        return {"check": "orphan_segments", "status": "ok", "detail": "no chroma database found"}

    try:
        import sqlite3
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM segments")
            valid_ids = {row[0] for row in cur.fetchall()}
    except Exception as e:
        return {"check": "orphan_segments", "status": "warn",
                "detail": f"could not query segments table: {e}"}

    dirs = [d for d in chroma_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
    orphans = [d for d in dirs if d.name not in valid_ids]

    if not orphans and not abandoned_dirs:
        return {"check": "orphan_segments", "status": "ok",
                "detail": "no orphan segment directories on disk"}

    total_bytes = 0
    for o in orphans:
        try:
            total_bytes += sum(f.stat().st_size for f in o.rglob("*") if f.is_file())
        except Exception:
            pass
    for d in abandoned_dirs:
        try:
            total_bytes += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        except Exception:
            pass

    size_mb = total_bytes / (1024 * 1024)
    details = []
    if orphans:
        details.append(f"{len(orphans)} orphan segment directory(ies)")
    if abandoned_dirs:
        details.append(f"{len(abandoned_dirs)} abandoned legacy index directory(ies)")
    return {
        "check": "orphan_segments",
        "status": "warn",
        "detail": f"{', '.join(details)} ({size_mb:.1f} MB)",
        "fix": "run: delegation-core doctor --clean-orphans",
    }


def clean_orphan_segments(cfg) -> int:
    """Remove orphan segment directories on disk that are not referenced in SQLite."""
    import shutil
    import sqlite3
    removed = 0

    chroma_dir = cfg.chroma_path
    if cfg.vault and cfg.vault.is_dir():
        abandoned_dirs = [
            p for p in cfg.vault.glob(".chroma*")
            if p.is_dir() and p.resolve() != chroma_dir.resolve()
        ]
        for d in abandoned_dirs:
            try:
                shutil.rmtree(d)
                removed += 1
            except Exception:
                pass

    db_path = chroma_dir / "chroma.sqlite3"
    if db_path.exists():
        try:
            with sqlite3.connect(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT id FROM segments")
                valid_ids = {row[0] for row in cur.fetchall()}
            dirs = [d for d in chroma_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
            orphans = [d for d in dirs if d.name not in valid_ids]
            for o in orphans:
                try:
                    shutil.rmtree(o)
                    removed += 1
                except Exception:
                    pass
        except Exception:
            pass
    return removed


def check_fts_integrity(cfg) -> dict:
    """Check whether Chroma SQLite FTS5 fulltext search table is healthy or malformed."""
    db_path = cfg.chroma_path / "chroma.sqlite3"
    if not db_path.exists():
        return {"check": "fts_integrity", "status": "ok", "detail": "no chroma database found"}
    try:
        import sqlite3
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='embedding_fulltext_search'")
            if not cur.fetchall():
                return {"check": "fts_integrity", "status": "ok", "detail": "no full-text search table"}
            cur.execute("INSERT INTO embedding_fulltext_search(embedding_fulltext_search) VALUES('integrity-check')")
    except Exception as e:
        return {
            "check": "fts_integrity",
            "status": "error",
            "detail": f"full-text search index is corrupt: {e}",
            "fix": "run: delegation-core doctor --rebuild-fts",
        }
    return {"check": "fts_integrity", "status": "ok", "detail": "full-text search index is healthy"}


def rebuild_fts(cfg) -> bool:
    """Rebuild the Chroma SQLite FTS5 index from the documents table."""
    db_path = cfg.chroma_path / "chroma.sqlite3"
    if not db_path.exists():
        return False
    try:
        import sqlite3
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO embedding_fulltext_search(embedding_fulltext_search) VALUES('rebuild')")
            conn.commit()
        return True
    except Exception:
        return False


def check_local_fallback(cfg) -> dict:
    """A sentinela `no_auto_reindex` e o fallback em processo, juntos.

    `allow_local_index_fallback` tem default `True` e trabalha em par com essa
    sentinela: quem cria o arquivo esta dizendo "nao reindexe sozinho", em geral
    porque quer que o daemon seja o unico escritor do indice. Mas o default `True`
    faz o fallback em processo voltar sozinho pelo outro caminho: uma CLI que nao
    encontra o daemon abre o proprio ChromaDB e carrega o proprio BGE.

    Numa maquina com placa unica isso e a contencao que a v0.11 eliminou, e numa
    com o daemon no ar e o segundo escritor e a corrupcao latente do indice. As
    duas configuracoes sao legitimas isoladas; juntas quase sempre nao sao o que
    a pessoa quis dizer, e nada avisava.
    """
    sentinela = CONFIG_DIR / "no_auto_reindex"
    if not sentinela.exists():
        return {"check": "local_fallback", "status": "ok",
                "detail": "no sentry file; automatic reindex is allowed"}
    if not getattr(cfg, "allow_local_index_fallback", True):
        return {"check": "local_fallback", "status": "ok",
                # Dizia "the daemon is the only writer, consistently". Essas
                # duas configuracoes governam o caminho de FALLBACK -- uma CLI
                # que NAO ALCANCA o daemon -- e nao dizem nada sobre um comando
                # que nunca tenta o daemon. Em 04/09/2026 quatro comandos
                # escreviam por fora e este check estava verde. Quem confere a
                # propriedade que a frase antiga prometia e check_index_writers.
                "detail": "sentry present and local fallback off: a CLI that "
                          "cannot reach the daemon will not open the index "
                          "itself"}
    return {
        "check": "local_fallback", "status": "warn",
        "detail": (f"{sentinela.name} says do not reindex automatically, but "
                   "allow_local_index_fallback is on, so a CLI that cannot reach "
                   "the daemon still opens its own index and loads its own BGE"),
        "fix": 'set "allow_local_index_fallback": false in config.json, or remove '
               f"{sentinela}",
    }



def _caminho_do_cli():
    """Onde mora o cli.py DESTA instalacao. Separado para o teste apontar noutro."""
    return Path(__file__).with_name("cli.py")


def check_index_writers() -> dict:
    """Todo comando de CLI que abre o indice passa pelo daemon?

    Existe porque `check_local_fallback` respondia essa pergunta sem te-la
    verificado. A frase dele era "the daemon is the only writer, consistently",
    e o que ele conferia eram duas chaves de configuracao que governam o caminho
    de fallback. Rodado nesta maquina em 04/09/2026 o doctor devolveu 10 ok e 0
    avisos enquanto `note write`, `note update`, `relink` e `graph build`
    abriam o ChromaDB no proprio processo, com o daemon segurando o mesmo
    arquivo -- medido com lsof.

    Le o `cli.py` da INSTALACAO, e nao do repositorio, que e o que torna o check
    util: pega uma instalacao desatualizada, ou remendada a mao, onde um comando
    ainda escreve por fora. `tests/test_cli_delega_escrita.py` guarda a mesma
    propriedade no repo, na hora de escrever o codigo; este guarda na maquina
    de quem instalou.

    Um `cli.py` que nao pode ser lido responde "nao da para saber" e nao "ok":
    e a regra que este proprio check nasceu para consertar.
    """
    import ast

    #: Isentos, com o motivo medido. Espelha _ISENTOS do teste do repo.
    isentos = {"cmd_note_read", "cmd_note_list", "cmd_doctor"}
    caminho = _caminho_do_cli()
    try:
        fonte = caminho.read_text(encoding="utf-8")
        arvore = ast.parse(fonte)
    except Exception as e:
        return {"check": "index_writers", "status": "warn",
                "detail": f"could not read the installed CLI at {caminho} ({e}), "
                          "so whether every command routes through the daemon is "
                          "unknown"}

    linhas = fonte.splitlines()
    faltando = []
    for no in ast.walk(arvore):
        if not (isinstance(no, ast.FunctionDef) and no.name.startswith("cmd_")):
            continue
        corpo = "\n".join(linhas[no.lineno - 1: no.end_lineno])
        if "VaultManager(" not in corpo or no.name in isentos:
            continue
        if "_delegate(" not in corpo:
            faltando.append(no.name)

    if faltando:
        return {
            "check": "index_writers", "status": "warn",
            "detail": (f"{len(faltando)} CLI command(s) open the index in their own "
                       f"process without asking the daemon: {', '.join(sorted(faltando))}. "
                       "With the daemon up that is a second writer on the same "
                       "SQLite file, and a second copy of the embedding model."),
            "fix": "upgrade this installation: delegation-core update",
        }
    return {"check": "index_writers", "status": "ok",
            "detail": "every CLI command that opens the index routes through the daemon"}


def check_devices(cfg, tem_cuda=None) -> dict:
    """Onde roda cada modelo, e se os dois vao disputar a mesma placa.

    Os dois na GPU funcionam, mas o arbitro (gpu.py) alterna: cada busca depois
    de uma geracao recarrega o BGE, e cada geracao depois de uma busca recarrega
    o modelo. Numa placa de 16 GB os dois nao cabem juntos.
    """
    from .embeddings import detect_device
    from .engine import llama_device

    embed = (getattr(cfg, "embed_device", "auto") or "auto").strip().lower()
    if embed == "auto":
        embed = detect_device() if tem_cuda is None else ("cuda" if tem_cuda() else "cpu")
    modelo = llama_device(cfg)
    if cfg.is_agent_mode:
        modelo_desc = "nenhum (modo agente)"
    else:
        modelo_desc = modelo
    detail = f"embeddings: {embed} · modelo local: {modelo_desc}"
    disputa = (embed == "cuda" and not cfg.is_agent_mode and modelo != "cpu")
    if disputa:
        return {"check": "devices", "status": "warn",
                "detail": detail + "; os dois disputam a GPU e se alternam nela",
                "fix": 'ponha um na CPU: "embed_device": "cpu" (o BGE perde pouco) '
                       'ou "llama_device": "cpu"'}
    return {"check": "devices", "status": "ok", "detail": detail}


def run_all(cfg) -> dict:
    """Run every check. Returns {status, counts, checks[]} with the worst status on top."""
    checks = [
        check_engine_mode(cfg),
        check_devices(cfg),
        check_vault_folders(cfg),
        check_hooks(),
        check_graph_extra(),
        check_ingest_registry(),
        check_graph_registry(cfg),
        check_orphan_segments(cfg),
        check_fts_integrity(cfg),
        check_index_integrity(cfg),
        check_index_location(cfg),
        check_local_fallback(cfg),
        check_index_writers(),
    ]
    counts = {s: sum(1 for c in checks if c["status"] == s)
              for s in ("ok", "warn", "error", "skip")}
    overall = "error" if counts["error"] else "warn" if counts["warn"] else "ok"
    return {"status": overall, "counts": counts, "checks": checks}
