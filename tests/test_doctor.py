"""doctor: the checks that would have caught conditions which went unnoticed.

Each check here is retrospective. On a live machine:

* the installed SessionEnd hook had drifted behind the repo, so a shipped fix
  simply was not running;
* that hook wrote transcripts into a lowercase ``sessions/`` beside the vault's
  configured ``Sessions/`` — 29 files over seven weeks, invisible to indexing,
  search and health accounting, with no error anywhere;
* the [graph] extra had never been installed, so graph_build failed on an import;
* ``graphify`` had been built before vault_paths tracking, so a rebuild could not
  clean up its 599 filed notes — doctor found this on its very first run.

None of them raise; each returns ok | warn | error plus a one-line fix.
"""

import json
import sys
import subprocess

import pytest

from delegation_core import doctor
from delegation_core.config import Config


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    import delegation_core.config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "cfgdir")
    monkeypatch.setattr(doctor, "CONFIG_DIR", tmp_path / "cfgdir")
    monkeypatch.setattr(doctor, "INSTALLED_HOOKS", tmp_path / "cfgdir" / "hooks")
    (tmp_path / "cfgdir").mkdir()
    c = Config(vault_path=str(tmp_path / "vault"),
               vault_folders=["Reference", "Sessions", "Decisions"])
    for f in c.vault_folders:
        (c.vault / f).mkdir(parents=True)
    return c


def _caixa_e_significativa(base) -> bool:
    """Sonda o sistema de arquivos em vez de deduzir pelo nome do sistema.

    Um mac pode ter volume case-sensitive e um Linux pode montar um que nao
    seja, entao `platform.system()` responde a pergunta errada. Criar e
    consultar e a unica resposta que vale para o diretorio onde o teste vai
    de fato escrever.
    """
    sonda = base / "_SondaDeCaixa"
    sonda.mkdir()
    try:
        return not (base / "_sondadecaixa").exists()
    finally:
        sonda.rmdir()


@pytest.fixture
def pasta_sombra(cfg):
    """A pasta `sessions/` ao lado da `Sessions/` configurada.

    Este cenario nao pode existir onde a caixa nao e significativa: o `mkdir`
    levanta `FileExistsError` sobre a propria pasta configurada. Em APFS, que e
    case-insensitive por padrao, os dois testes que dependem dele falhavam com
    `[Errno 17] File exists`, duas falhas que nao diziam nada sobre o `doctor`.

    Pulado onde nao cabe, e identico ao que era onde cabe.
    """
    if not _caixa_e_significativa(cfg.vault):
        pytest.skip("sistema de arquivos case-insensitive: a pasta-sombra nao "
                    "pode existir aqui, e o defeito que estes testes cobrem "
                    "tambem nao")
    stray = cfg.vault / "sessions"
    stray.mkdir()
    return stray


# ── vault folders ────────────────────────────────────────────────────────────

def test_case_variant_folder_is_an_error_with_the_invisible_note_count(cfg, pasta_sombra):
    """The exact live bug: a lowercase sessions/ beside the configured Sessions/,
    holding notes that no indexed path ever reaches."""
    stray = pasta_sombra
    for i in range(3):
        (stray / f"t{i}.md").write_text("x", encoding="utf-8")

    result = doctor.check_vault_folders(cfg)

    assert result["status"] == "error"
    assert "sessions/ shadows Sessions/" in result["detail"]
    assert "3 note(s) invisible" in result["detail"]


def test_internal_underscore_folders_are_not_treated_as_shadows(cfg):
    for name in ("_inbox", "_processed", "_failed"):
        (cfg.vault / name).mkdir()

    assert doctor.check_vault_folders(cfg)["status"] == "ok"


def test_unrelated_folder_is_not_a_shadow(cfg):
    (cfg.vault / "Attachments").mkdir()

    assert doctor.check_vault_folders(cfg)["status"] == "ok"


def test_missing_configured_folder_is_only_a_warning(cfg):
    (cfg.vault / "Decisions").rmdir()

    result = doctor.check_vault_folders(cfg)

    assert result["status"] == "warn" and "Decisions" in result["detail"]


def test_absent_vault_is_an_error(cfg, tmp_path):
    cfg.vault_path = str(tmp_path / "nowhere")

    assert doctor.check_vault_folders(cfg)["status"] == "error"


# ── hook drift ───────────────────────────────────────────────────────────────

def test_hook_drift_detects_a_stale_installed_copy(cfg, tmp_path, monkeypatch):
    repo = tmp_path / "repo_hooks"
    repo.mkdir()
    (repo / "session_export.py").write_text("new version\n", encoding="utf-8")
    installed = tmp_path / "cfgdir" / "hooks"
    installed.mkdir()
    (installed / "session_export.py").write_text("old version\n", encoding="utf-8")
    monkeypatch.setattr(doctor, "_repo_hooks_dir", lambda: repo)

    result = doctor.check_hook_drift()

    assert result["status"] == "warn"
    assert "session_export.py" in result["detail"]
    assert result["fix"].startswith("cp ")


def test_hook_drift_reports_ok_when_bytes_match(cfg, tmp_path, monkeypatch):
    repo = tmp_path / "repo_hooks"
    repo.mkdir()
    (repo / "h.py").write_text("same\n", encoding="utf-8")
    installed = tmp_path / "cfgdir" / "hooks"
    installed.mkdir()
    (installed / "h.py").write_text("same\n", encoding="utf-8")
    monkeypatch.setattr(doctor, "_repo_hooks_dir", lambda: repo)

    assert doctor.check_hook_drift()["status"] == "ok"


def test_hook_drift_skips_cleanly_on_a_wheel_install(cfg, monkeypatch):
    monkeypatch.setattr(doctor, "_repo_hooks_dir", lambda: None)

    assert doctor.check_hook_drift()["status"] == "skip"


# ── registries ───────────────────────────────────────────────────────────────

def test_ingest_registry_flags_paths_that_no_longer_exist(cfg, tmp_path):
    (tmp_path / "cfgdir" / "ingested_sources.json").write_text(
        json.dumps({"/gone/forever": {"indexed_count": 3}}), encoding="utf-8")

    result = doctor.check_ingest_registry()

    assert result["status"] == "warn" and "ingest_forget" in result["fix"]


def test_graph_registry_flags_a_graph_built_before_vault_path_tracking(cfg, tmp_path):
    """This is what the first real doctor run found, on `graphify`."""
    cfg.graphs_dir.mkdir(parents=True, exist_ok=True)
    cfg.graphs_registry_path.write_text(json.dumps({
        "graphify": {"source_path": str(tmp_path), "node_count": 9594},
    }), encoding="utf-8")

    result = doctor.check_graph_registry(cfg)

    assert result["status"] == "warn"
    assert "graphify" in result["detail"]


def test_graph_registry_is_ok_when_sources_exist_and_paths_are_tracked(cfg, tmp_path):
    cfg.graphs_dir.mkdir(parents=True, exist_ok=True)
    cfg.graphs_registry_path.write_text(json.dumps({
        "ok-graph": {"source_path": str(tmp_path), "node_count": 10, "vault_paths": ["a.md"]},
    }), encoding="utf-8")

    assert doctor.check_graph_registry(cfg)["status"] == "ok"


# ── engine mode ──────────────────────────────────────────────────────────────

def test_agent_mode_never_complains_about_a_missing_model(cfg):
    cfg.engine_mode = "agent"
    cfg.llama_binary = "/nonexistent/llama-server"
    cfg.llama_model = "/nonexistent/model.gguf"

    assert doctor.check_engine_mode(cfg)["status"] == "ok"


def test_local_mode_with_a_missing_model_is_an_error(cfg):
    cfg.engine_mode = "local"
    cfg.llama_model = "/nonexistent/model.gguf"

    result = doctor.check_engine_mode(cfg)

    assert result["status"] == "error" and "llama_model" in result["detail"]


# ── index integrity ──────────────────────────────────────────────────────────

def _fake_probe(monkeypatch, cfg, *, stdout="", returncode=0, stderr=""):
    """Replace the child-process probe; check_index_integrity only reads its result."""
    cfg.chroma_path.mkdir(parents=True, exist_ok=True)
    (cfg.chroma_path / "chroma.sqlite3").write_bytes(b"")

    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    return seen


def test_a_scope_that_cannot_be_queried_is_an_error(monkeypatch, cfg):
    """The live bug: unfiltered search kept answering while every scope-filtered
    query died on "Error finding id", so the hand-written slice was unreachable."""
    _fake_probe(monkeypatch, cfg, stdout=json.dumps(
        {"broken": ["{'kind': 'note'}: RuntimeError: Error finding id"], "count": 10}))

    result = doctor.check_index_integrity(cfg)

    assert result["status"] == "error"
    assert "scope-filtered search fails" in result["detail"]
    assert "kind': 'note'" in result["detail"]
    assert "restart the MCP server" in result["fix"]


def test_a_probe_killed_by_a_signal_is_reported_not_propagated(monkeypatch, cfg):
    """The regression this design exists for: querying Chroma from this process
    segfaulted the whole CLI (exit 139) once a bulk ingest left ~879 uncompacted
    rows in its embeddings_queue. doctor must survive the condition it reports."""
    _fake_probe(monkeypatch, cfg, returncode=-11)

    result = doctor.check_index_integrity(cfg)

    assert result["status"] == "error"
    assert "signal 11" in result["detail"]


def test_a_crashing_index_does_not_send_the_reader_to_reindex(monkeypatch, cfg):
    """Measured on the live vault: `reindex --force` segfaults on this same state,
    so naming it as the fix sends someone into a second crash. What matters first
    is not restarting the server that is still serving from memory."""
    _fake_probe(monkeypatch, cfg, returncode=-11)

    fix = doctor.check_index_integrity(cfg)["fix"]

    assert "do not restart" in fix
    assert "reindex --force crashes" in fix
    assert "recover-index" in fix


def test_a_hanging_probe_is_bounded(monkeypatch, cfg):
    cfg.chroma_path.mkdir(parents=True, exist_ok=True)
    (cfg.chroma_path / "chroma.sqlite3").write_bytes(b"")

    def hang(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 120))

    monkeypatch.setattr(doctor.subprocess, "run", hang)

    result = doctor.check_index_integrity(cfg)

    assert result["status"] == "warn"
    assert "120s" in result["detail"]


def test_the_probe_runs_out_of_process_with_every_scope(monkeypatch, cfg):
    seen = _fake_probe(monkeypatch, cfg,
                       stdout=json.dumps({"broken": [], "count": 1}))

    doctor.check_index_integrity(cfg)

    assert seen["argv"][0] == sys.executable
    assert json.loads(seen["argv"][-1]) == [dict(f) for f in doctor._SCOPE_FILTERS]
    assert seen["kwargs"]["timeout"] == 120


def test_the_probed_filters_are_the_ones_search_actually_sends():
    """is_external is written and queried as the string "true"; probing the
    boolean would match no row and pass without testing anything."""
    from delegation_core.vault import VaultManager

    sent = []

    class Spy:
        def query(self, **kwargs):
            sent.append(kwargs.get("where"))
            return {"documents": [[]], "metadatas": [[]], "distances": [[]]}

    vm = VaultManager(Config(vault_path="/tmp/whatever"))
    vm._ensure_ready = lambda: None
    vm.collection = Spy()
    for scope in ("notes", "generated", "external"):
        vm.search("q", scope=scope)

    assert sent == list(doctor._SCOPE_FILTERS)


def test_a_working_index_reports_its_size(monkeypatch, cfg):
    _fake_probe(monkeypatch, cfg, stdout=json.dumps({"broken": [], "count": 5687}))

    result = doctor.check_index_integrity(cfg)

    assert result["status"] == "ok"
    assert "5687 row(s)" in result["detail"]


def test_the_probe_never_loads_an_embedding_model():
    """A doctor run must stay cheap: the probe carries its own constant vector,
    so no embedding function is ever constructed."""
    assert "embedding_function=" not in doctor._PROBE_SOURCE
    assert "get_collection(name=name)" in doctor._PROBE_SOURCE


def test_no_index_yet_is_not_a_complaint(cfg):
    assert doctor.check_index_integrity(cfg)["status"] == "ok"


def test_a_collection_with_no_vectors_skips(monkeypatch, cfg):
    _fake_probe(monkeypatch, cfg, stdout=json.dumps({"empty": True}))

    assert doctor.check_index_integrity(cfg)["status"] == "skip"


def test_an_unreadable_index_warns_instead_of_raising(monkeypatch, cfg):
    _fake_probe(monkeypatch, cfg, returncode=1,
                stderr="RuntimeError: file is not a database")

    result = doctor.check_index_integrity(cfg)

    assert result["status"] == "warn"
    assert "file is not a database" in result["detail"]
    assert "reindex" in result["fix"]


def test_unparseable_probe_output_skips_rather_than_guessing(monkeypatch, cfg):
    _fake_probe(monkeypatch, cfg, stdout="")

    assert doctor.check_index_integrity(cfg)["status"] == "skip"


# ── orphan segments ──────────────────────────────────────────────────────────

def test_orphan_segments_detected_and_cleaned(cfg, tmp_path):
    import sqlite3
    chroma_dir = cfg.chroma_path
    chroma_dir.mkdir(parents=True, exist_ok=True)
    db_path = chroma_dir / "chroma.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE segments (id TEXT PRIMARY KEY, type TEXT, scope TEXT, collection TEXT)")
        conn.execute("INSERT INTO segments VALUES ('valid-uuid-1', 'type', 'scope', 'col-1')")

    (chroma_dir / "valid-uuid-1").mkdir()
    (chroma_dir / "valid-uuid-1" / "data.bin").write_bytes(b"1234")

    stale_dir = chroma_dir / "orphan-uuid-2"
    stale_dir.mkdir()
    (stale_dir / "old.bin").write_bytes(b"5678")

    result = doctor.check_orphan_segments(cfg)
    assert result["status"] == "warn"
    assert "1 orphan segment" in result["detail"]
    assert "clean-orphans" in result["fix"]

    cleaned = doctor.clean_orphan_segments(cfg)
    assert cleaned == 1
    assert not stale_dir.exists()
    assert (chroma_dir / "valid-uuid-1").exists()

    result_clean = doctor.check_orphan_segments(cfg)
    assert result_clean["status"] == "ok"


# ── fts integrity ────────────────────────────────────────────────────────────

def test_fts_integrity_detected_and_rebuilt(cfg, tmp_path):
    import sqlite3
    chroma_dir = cfg.chroma_path
    chroma_dir.mkdir(parents=True, exist_ok=True)
    db_path = chroma_dir / "chroma.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE VIRTUAL TABLE embedding_fulltext_search USING fts5(string_value)")
        conn.execute("INSERT INTO embedding_fulltext_search VALUES ('some content')")

    result = doctor.check_fts_integrity(cfg)
    assert result["status"] == "ok"
    assert "healthy" in result["detail"]

    rebuilt = doctor.rebuild_fts(cfg)
    assert rebuilt is True


# ── aggregate ────────────────────────────────────────────────────────────────

def test_run_all_surfaces_the_worst_status(cfg, pasta_sombra):
    (pasta_sombra / "a.md").write_text("x", encoding="utf-8")

    result = doctor.run_all(cfg)

    assert result["status"] == "error"
    assert result["counts"]["error"] >= 1
    assert {c["check"] for c in result["checks"]} >= {"engine_mode", "vault_folders", "hook_drift", "orphan_segments", "fts_integrity"}


# ── sentinela e fallback, que se contradizem em silencio ────────────────────

def _sentinela(cfg, ligada: bool):
    alvo = doctor.CONFIG_DIR / "no_auto_reindex"
    alvo.parent.mkdir(parents=True, exist_ok=True)
    if ligada:
        alvo.touch()
    else:
        alvo.unlink(missing_ok=True)
    return alvo


def test_sem_sentinela_esta_tudo_bem(cfg):
    _sentinela(cfg, False)
    assert doctor.check_local_fallback(cfg)["status"] == "ok"


def test_sentinela_com_fallback_ligado_e_aviso(cfg):
    """As duas configuracoes sao legitimas isoladas. Juntas, quem pediu para o
    daemon ser o unico escritor recebe um segundo escritor pelo outro caminho."""
    _sentinela(cfg, True)
    cfg.allow_local_index_fallback = True
    r = doctor.check_local_fallback(cfg)

    assert r["status"] == "warn"
    assert "allow_local_index_fallback" in r["detail"]
    assert "fix" in r


def test_sentinela_com_fallback_desligado_e_coerente(cfg):
    _sentinela(cfg, True)
    cfg.allow_local_index_fallback = False
    assert doctor.check_local_fallback(cfg)["status"] == "ok"


def test_o_check_entra_no_run_all(cfg):
    nomes = {c["check"] for c in doctor.run_all(cfg)["checks"]}
    assert "local_fallback" in nomes


def test_o_aviso_nao_derruba_o_status_geral_para_erro(cfg):
    """E aviso, nao erro: a maquina funciona, so nao faz o que a pessoa achou
    que tinha pedido."""
    _sentinela(cfg, True)
    cfg.allow_local_index_fallback = True
    assert doctor.check_local_fallback(cfg)["status"] != "error"
