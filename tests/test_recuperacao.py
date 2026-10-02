"""O indice que derruba quem o abre se conserta sozinho (recuperacao.py).

Os dois Macs de 29/09/2026: SIGSEGV em chromadb_rust_bindings em todo processo
que abria o indice, o launchd reiniciando o daemon em ciclo, e o unico conserto
sendo manual. Estes testes cobrem as tres partes do conserto: reconhecer que o
processo anterior morreu abrindo o indice, confirmar numa sonda antes de mexer
em qualquer coisa, e reconstruir a partir das fontes sem que carimbos antigos
facam o reindex pular tudo.

A morte por sinal da sonda e simulada: provocar um SIGSEGV de verdade no
chromadb dependeria de um indice danificado que ninguem sabe fabricar. O resto
(renomear, zerar carimbos, abrir um indice novo com o chromadb real, retomar a
reconstrucao) e o comportamento real.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from delegation_core import config as config_mod
from delegation_core import doctor, gpu, ingest, jobs, notes, recuperacao
from delegation_core.config import Config
from delegation_core.vault import VaultManager


def _pid_morto() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _cfg(tmp_path, nome_do_vault="vault") -> Config:
    vault = tmp_path / nome_do_vault
    (vault / "Notes").mkdir(parents=True)
    return Config(vault_path=str(vault), vault_folders=["Notes"], search_threshold=0.0)


def _indice_falso(cfg) -> None:
    cfg.chroma_path.mkdir(parents=True)
    (cfg.chroma_path / "chroma.sqlite3").write_bytes(b"danificado")


def _marcador_de(pid, cfg) -> None:
    recuperacao._gravar_json(recuperacao.caminho_do_marcador(),
                             {"pid": pid, "path": str(cfg.chroma_path), "inicio": "x"})


def _sonda(monkeypatch, sinal):
    chamadas = []

    def sondar(cfg, timeout=120):
        chamadas.append(cfg.chroma_path)
        return {"sinal": sinal}

    monkeypatch.setattr(doctor, "sondar_indice", sondar)
    return chamadas


def _sonda_proibida(monkeypatch):
    """Registra em vez de levantar: `antes_de_abrir` engole toda excecao, de
    proposito, entao uma sonda que levanta passaria calada. Os testes conferem
    a lista vazia no fim."""
    return _sonda(monkeypatch, sinal=11)


# ── quando a sonda roda, e quando nao ────────────────────────────────────────

def test_sem_marcador_nao_sonda_nem_mexe(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _indice_falso(cfg)
    chamadas = _sonda_proibida(monkeypatch)

    assert recuperacao.antes_de_abrir(cfg) is False
    assert chamadas == []
    assert (cfg.chroma_path / "chroma.sqlite3").exists()
    assert json.loads(recuperacao.caminho_do_marcador().read_text())["pid"] == __import__("os").getpid()


def test_marcador_de_processo_vivo_nao_e_morte(tmp_path, monkeypatch):
    """Outro processo abrindo o mesmo indice agora (uma CLI com o daemon no ar)
    deixa marcador de PID vivo. Isso nao e morte, e nao pode disparar nada."""
    cfg = _cfg(tmp_path)
    _indice_falso(cfg)
    _marcador_de(__import__("os").getppid(), cfg)
    chamadas = _sonda_proibida(monkeypatch)

    assert recuperacao.antes_de_abrir(cfg) is False
    assert chamadas == []
    assert cfg.chroma_path.exists()


def test_marcador_de_outro_indice_nao_conta(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _indice_falso(cfg)
    recuperacao._gravar_json(recuperacao.caminho_do_marcador(),
                             {"pid": _pid_morto(), "path": "/outro/indice"})
    chamadas = _sonda_proibida(monkeypatch)

    assert recuperacao.antes_de_abrir(cfg) is False
    assert chamadas == []


def test_morte_anterior_com_sonda_saudavel_nao_poe_em_quarentena(tmp_path, monkeypatch):
    """A morte teve outra causa (OOM, kill a mao). Quarentena por engano custa
    uma reconstrucao inteira, entao a sonda saudavel manda seguir."""
    cfg = _cfg(tmp_path)
    _indice_falso(cfg)
    _marcador_de(_pid_morto(), cfg)
    chamadas = _sonda(monkeypatch, sinal=None)

    assert recuperacao.antes_de_abrir(cfg) is False
    assert chamadas == [cfg.chroma_path]
    assert (cfg.chroma_path / "chroma.sqlite3").read_bytes() == b"danificado"
    assert recuperacao.reconstrucao_pendente() is None


def test_guarda_que_falha_nao_impede_a_abertura(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _indice_falso(cfg)
    _marcador_de(_pid_morto(), cfg)

    def explode(*_a, **_k):
        raise RuntimeError("sonda quebrada")
    monkeypatch.setattr(doctor, "sondar_indice", explode)

    assert recuperacao.antes_de_abrir(cfg) is False
    assert cfg.chroma_path.exists()


# ── a quarentena ─────────────────────────────────────────────────────────────

def test_morte_confirmada_pela_sonda_poe_em_quarentena(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _indice_falso(cfg)
    _marcador_de(_pid_morto(), cfg)
    _sonda(monkeypatch, sinal=11)

    assert recuperacao.antes_de_abrir(cfg) is True

    assert not cfg.chroma_path.exists()
    guardados = list(cfg.vault.glob(".chroma_bge-danificado-*"))
    assert len(guardados) == 1, "o indice antigo tem que ser renomeado, nao apagado"
    assert (guardados[0] / "chroma.sqlite3").read_bytes() == b"danificado"
    pedido = recuperacao.reconstrucao_pendente()
    assert pedido["quarentena"] == str(guardados[0])
    assert pedido["notas_feitas"] is False


def test_quarentena_zera_os_carimbos_do_vault(tmp_path):
    """Sem isto o reindex incremental pula toda nota cujo mtime bate com o
    carimbo, e o indice novo nasce vazio com cara de saudavel."""
    cfg = _cfg(tmp_path)
    _indice_falso(cfg)
    estado = notes.caminho_do_estado(cfg.vault)
    notes.gravar_estado(estado, {"Notes/a.md": 123.0, "__schema__": 3})

    recuperacao.pos_em_quarentena(cfg, motivo="teste")

    assert notes.carregar_estado(estado) == {}


def test_quarentena_zera_os_arquivos_mas_guarda_como_reingerir(tmp_path):
    cfg = _cfg(tmp_path)
    _indice_falso(cfg)
    fonte = tmp_path / "docs"
    fonte.mkdir()
    ingest._save_registry({str(fonte): {
        "recursive": False, "exclude": ["*.log"],
        "files": {str(fonte / "a.md"): [1.0, 10]}}})

    pedido = recuperacao.pos_em_quarentena(cfg, motivo="teste")

    registro = ingest._load_registry()
    assert registro[str(fonte)]["files"] == {}
    assert registro[str(fonte)]["exclude"] == ["*.log"]
    assert pedido["fontes"] == [{"path": str(fonte), "recursive": False,
                                 "exclude": ["*.log"]}]


def test_vault_sincronizado_manda_o_indice_novo_para_fora(tmp_path):
    cfg = _cfg(tmp_path, "OneDrive - Soteria/Business_Vault")
    _indice_falso(cfg)
    config_mod.CONFIG_FILE.write_text(json.dumps({"vault_path": cfg.vault_path}))

    pedido = recuperacao.pos_em_quarentena(cfg, motivo="teste")

    local = recuperacao.caminho_local_do_indice()
    assert cfg.chroma_path == local
    assert pedido["indice_novo"] == str(local)
    assert pedido["relocado_para_fora_da_nuvem"] is True
    assert json.loads(config_mod.CONFIG_FILE.read_text())["index_path"] == str(local)


def test_vault_local_reconstroi_no_mesmo_lugar(tmp_path):
    cfg = _cfg(tmp_path)
    _indice_falso(cfg)

    pedido = recuperacao.pos_em_quarentena(cfg, motivo="teste")

    assert cfg.index_path == ""
    assert pedido["indice_novo"] == str(cfg.vault / ".chroma_bge")


def test_novo_caminho_explicito_vence(tmp_path):
    cfg = _cfg(tmp_path)
    _indice_falso(cfg)
    alvo = tmp_path / "longe" / "indice"

    recuperacao.pos_em_quarentena(cfg, motivo="teste", novo_caminho=str(alvo))

    assert cfg.chroma_path == alvo


@pytest.mark.parametrize("caminho,esperado", [
    ("/Users/saad/Library/CloudStorage/OneDrive-Soteria/Business_Vault/.chroma_bge", True),
    ("/Users/x/Library/Mobile Documents/com~apple~CloudDocs/vault/.chroma_bge", True),
    ("/home/x/Dropbox/vault/.chroma_bge", True),
    ("C:/Users/x/OneDrive - Empresa/vault/.chroma_bge", True),
    ("/Users/x/Work_Vault/.chroma_bge", False),
    ("/home/joey/Documents/Projects_Archive/Claude Vault/.chroma_bge", False),
])
def test_deteccao_de_pasta_sincronizada(caminho, esperado):
    assert recuperacao.em_pasta_sincronizada(Path(caminho)) is esperado


# ── pelo caminho real de abertura ────────────────────────────────────────────

class _Embedder:
    def __call__(self, input):
        return [[1.0] + [0.0] * 7 for _ in input]

    def embed_documents(self, input):
        return self(input)

    def embed_query(self, input):
        return self([input])[0] if isinstance(input, str) else self(list(input))

    def name(self):
        return "probe"


def test_o_daemon_que_sobe_depois_da_morte_abre_um_indice_novo(tmp_path, monkeypatch):
    """O ciclo do launchd, do lado de quem sobe: marcador de um PID morto, sonda
    que morre por sinal, e a abertura do chromadb real num indice limpo."""
    monkeypatch.setattr(gpu, "take", lambda *a, **k: None)
    cfg = _cfg(tmp_path)
    _indice_falso(cfg)
    _marcador_de(_pid_morto(), cfg)
    _sonda(monkeypatch, sinal=11)

    vm = VaultManager(cfg)
    vm.ef = _Embedder()
    vm._init()
    try:
        assert vm._initialized
        assert (cfg.chroma_path / "chroma.sqlite3").stat().st_size > len(b"danificado")
        assert list(cfg.vault.glob(".chroma_bge-danificado-*"))
        assert not recuperacao.caminho_do_marcador().exists(), \
            "abertura bem sucedida tem que apagar o marcador"
        assert recuperacao.reconstrucao_pendente() is not None
    finally:
        vm._close_client()


@pytest.mark.skipif(sys.platform == "win32", reason="SIGSEGV por os.kill e POSIX")
def test_sonda_que_morre_de_verdade_por_sigsegv_dispara_a_quarentena(tmp_path, monkeypatch):
    """Sem simular a sonda: o filho morre de verdade por SIGSEGV, e o caminho
    subprocess -> returncode negativo -> sinal -> quarentena roda inteiro."""
    monkeypatch.setattr(gpu, "take", lambda *a, **k: None)
    monkeypatch.setattr(doctor, "_PROBE_SOURCE",
                        "import os, signal\nos.kill(os.getpid(), signal.SIGSEGV)\n")
    cfg = _cfg(tmp_path)
    _indice_falso(cfg)
    _marcador_de(_pid_morto(), cfg)

    assert doctor.sondar_indice(cfg)["sinal"] == 11
    vm = VaultManager(cfg)
    vm.ef = _Embedder()
    vm._init()
    try:
        assert vm._initialized
        assert list(cfg.vault.glob(".chroma_bge-danificado-*"))
    finally:
        vm._close_client()


def test_sonda_que_trava_confirma_o_dano(tmp_path, monkeypatch):
    """O Mac de 29/09 13:39: `collection.count()` parado em mutexwait, o watchdog
    encerrando o daemon a cada 300s. A primeira versao da guarda so aceitava
    sinal, tomou o prazo esgotado por indice saudavel e deixou o ciclo girar.
    Aqui o filho trava de verdade e o prazo e que decide."""
    monkeypatch.setattr(gpu, "take", lambda *a, **k: None)
    monkeypatch.setattr(doctor, "_PROBE_SOURCE", "import time\ntime.sleep(60)\n")
    monkeypatch.setattr(recuperacao, "PRAZO_DA_SONDA", 1)
    cfg = _cfg(tmp_path)
    _indice_falso(cfg)
    _marcador_de(_pid_morto(), cfg)

    assert recuperacao.falha_da_sonda(cfg) == "nao abriu em 1s"
    vm = VaultManager(cfg)
    vm.ef = _Embedder()
    vm._init()
    try:
        assert vm._initialized
        assert list(cfg.vault.glob(".chroma_bge-danificado-*"))
        assert "nao abriu em 1s" in recuperacao.reconstrucao_pendente()["motivo"]
    finally:
        vm._close_client()


def test_sonda_com_erro_de_python_nao_poe_em_quarentena(tmp_path, monkeypatch):
    """Erro com saida normal nao derruba o processo: o `_init` ja tenta de novo
    na chamada seguinte. Nao e a condicao que justifica uma reconstrucao."""
    monkeypatch.setattr(doctor, "_PROBE_SOURCE", "raise SystemExit(3)\n")
    cfg = _cfg(tmp_path)
    _indice_falso(cfg)
    _marcador_de(_pid_morto(), cfg)

    assert recuperacao.falha_da_sonda(cfg) is None
    assert recuperacao.antes_de_abrir(cfg) is False
    assert (cfg.chroma_path / "chroma.sqlite3").exists()


@pytest.mark.parametrize("codigo", [0xC0000005, -1073741819])
def test_crash_no_windows_conta_como_sinal(tmp_path, monkeypatch, codigo):
    cfg = _cfg(tmp_path)
    _indice_falso(cfg)
    monkeypatch.setattr(doctor.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, codigo, "", ""))
    assert doctor.sondar_indice(cfg)["sinal"] is not None
    assert doctor.check_index_integrity(cfg)["status"] == "error"


def test_abertura_normal_nao_deixa_marcador(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu, "take", lambda *a, **k: None)
    cfg = _cfg(tmp_path)
    chamadas = _sonda_proibida(monkeypatch)

    vm = VaultManager(cfg)
    vm.ef = _Embedder()
    vm._init()
    try:
        assert vm._initialized
        assert not recuperacao.caminho_do_marcador().exists()
        assert chamadas == []
    finally:
        vm._close_client()


# ── a reconstrucao ───────────────────────────────────────────────────────────

class _VaultFalso:
    def __init__(self, aberto=True):
        self.collection = object() if aberto else None
        self.reindex = []

    def _ensure_ready(self):
        pass

    def reindex_vault(self, force=False):
        self.reindex.append(force)
        return 7


class _IngestFalso:
    def __init__(self):
        self.chamadas = []

    def ingest(self, path, recursive=True, force=False, exclude=None):
        self.chamadas.append((path, recursive, force, exclude))
        return {"source": path, "indexed": 1}


def _pedido(tmp_path, **extra):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    pedido = {"fontes": [{"path": str(a), "recursive": True, "exclude": []},
                         {"path": str(b), "recursive": False, "exclude": ["*.log"]},
                         {"path": str(tmp_path / "sumiu"), "recursive": True, "exclude": []}],
              "notas_feitas": False, "fontes_feitas": [], "quarentena": "/q"}
    pedido.update(extra)
    recuperacao._gravar_json(recuperacao.caminho_do_pedido(), pedido)
    return a, b


def test_reconstrucao_reindexa_tudo_e_reingere_cada_fonte(tmp_path):
    a, b = _pedido(tmp_path)
    vault, ing = _VaultFalso(), _IngestFalso()

    resultado = recuperacao.reconstruir(vault, ing)

    assert vault.reindex == [True], "tem que ser reindex completo"
    assert ing.chamadas == [(str(a), True, True, None), (str(b), False, True, ["*.log"])]
    assert "nao existe" in resultado["fontes"][2]["error"]
    assert recuperacao.reconstrucao_pendente() is None


def test_reconstrucao_retoma_de_onde_parou(tmp_path):
    a, b = _pedido(tmp_path)
    pedido = recuperacao.reconstrucao_pendente()
    pedido.update(notas_feitas=True, fontes_feitas=[str(a)])
    recuperacao._gravar_json(recuperacao.caminho_do_pedido(), pedido)
    vault, ing = _VaultFalso(), _IngestFalso()

    recuperacao.reconstruir(vault, ing)

    assert vault.reindex == []
    assert [c[0] for c in ing.chamadas] == [str(b)]


def test_reconstrucao_sobre_indice_que_nao_abriu_nao_se_apaga(tmp_path):
    _pedido(tmp_path)

    with pytest.raises(RuntimeError):
        recuperacao.reconstruir(_VaultFalso(aberto=False), _IngestFalso())

    pedido = recuperacao.reconstrucao_pendente()
    assert pedido is not None and pedido["notas_feitas"] is False


def test_reconstrucao_sem_pedido_nao_faz_nada():
    vault = _VaultFalso()
    assert recuperacao.reconstruir(vault, _IngestFalso())["status"] == "nada a reconstruir"
    assert vault.reindex == []


def test_reconstrucao_absorve_o_reindex_do_hook():
    """Mesma familia do reindex: o reindex que o hook de fim de sessao dispara
    durante a reconstrucao e absorvido por ela, em vez de correr junto."""
    assert jobs._familia("vault_reindex:reconstrucao") == jobs._familia("vault_reindex:incremental")
    assert "ingest_configured" in jobs.TAREFAS_EXCLUSIVAS


# ── doctor ───────────────────────────────────────────────────────────────────

def test_doctor_avisa_indice_em_pasta_sincronizada(tmp_path):
    cfg = _cfg(tmp_path, "OneDrive/vault")
    r = doctor.check_index_location(cfg)
    assert r["status"] == "warn"
    assert "recover-index --index-path" in r["fix"]


def test_doctor_nao_avisa_indice_local(tmp_path):
    assert doctor.check_index_location(_cfg(tmp_path))["status"] == "ok"


def test_index_path_muda_onde_o_indice_mora(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.chroma_path == cfg.vault / ".chroma_bge"
    cfg.index_path = str(tmp_path / "fora")
    assert cfg.chroma_path == tmp_path / "fora"


# ── cli ──────────────────────────────────────────────────────────────────────

def _args(**kw):
    import argparse
    base = {"index_path": "", "yes": True}
    base.update(kw)
    return argparse.Namespace(**base)


def test_recover_index_recusa_com_o_daemon_no_ar(tmp_path, monkeypatch):
    from delegation_core import cli, daemon
    cfg = _cfg(tmp_path)
    _indice_falso(cfg)
    monkeypatch.setattr(cli, "_graph_config", lambda: cfg)
    monkeypatch.setattr(daemon, "is_listening", lambda _cfg: True)

    with pytest.raises(SystemExit) as e:
        cli.cmd_recover_index(_args())

    assert e.value.code == 1
    assert (cfg.chroma_path / "chroma.sqlite3").exists()


def test_recover_index_com_novo_caminho_grava_a_config(tmp_path, monkeypatch):
    from delegation_core import cli, daemon
    cfg = _cfg(tmp_path)
    _indice_falso(cfg)
    config_mod.CONFIG_FILE.write_text(json.dumps({"vault_path": cfg.vault_path}))
    monkeypatch.setattr(cli, "_graph_config", lambda: cfg)
    monkeypatch.setattr(daemon, "is_listening", lambda _cfg: False)
    alvo = tmp_path / "local" / "indice"

    cli.cmd_recover_index(_args(index_path=str(alvo)))

    assert list(cfg.vault.glob(".chroma_bge-danificado-*"))
    assert json.loads(config_mod.CONFIG_FILE.read_text())["index_path"] == str(alvo.resolve())
    assert recuperacao.reconstrucao_pendente()["indice_novo"] == str(alvo.resolve())


# ── servidor ─────────────────────────────────────────────────────────────────

def test_partida_com_pedido_submete_a_reconstrucao(tmp_path, monkeypatch):
    from delegation_core import server
    _pedido(tmp_path)
    vault, ing = _VaultFalso(), _IngestFalso()
    monkeypatch.setattr(server, "_vault", vault)
    monkeypatch.setattr(server, "_ingest", ing)
    submetidos = []
    monkeypatch.setattr(server.jobs, "submit",
                        lambda nome, fn, *a: submetidos.append((nome, fn, a)) or "j1")

    server._retomar_reconstrucao_na_partida().join(5)

    assert [s[0] for s in submetidos] == ["vault_reindex:reconstrucao"]
    nome, fn, a = submetidos[0]
    assert fn is recuperacao.reconstruir and a == (vault, ing)


def test_partida_sem_pedido_nao_submete_nada(monkeypatch):
    from delegation_core import server
    monkeypatch.setattr(server, "_vault", _VaultFalso())
    monkeypatch.setattr(server, "_ingest", _IngestFalso())
    submetidos = []
    monkeypatch.setattr(server.jobs, "submit", lambda *a, **k: submetidos.append(a))

    server._retomar_reconstrucao_na_partida().join(5)

    assert submetidos == []


def _heartbeat(monkeypatch, tmp_path):
    import asyncio
    from delegation_core import server

    class Engine:
        cfg = Config(vault_path=str(tmp_path), engine_mode="agent")

    class Vault:
        def get_stats(self):
            return {}

        def get_health_summary(self, force=False):
            return {}

    class Tracker:
        def summary(self):
            return {}

    monkeypatch.setattr(server, "_engine", Engine())
    monkeypatch.setattr(server, "_vault", Vault())
    monkeypatch.setattr(server, "_tracker", Tracker())
    return json.loads(asyncio.run(server.heartbeat()))


def test_heartbeat_mostra_a_reconstrucao_e_se_diz_degradado(tmp_path, monkeypatch):
    """A busca de um indice em reconstrucao so parece pior. O heartbeat e o
    lugar onde um agente descobre que ela vai melhorar sozinha."""
    _pedido(tmp_path, motivo="teste")

    r = _heartbeat(monkeypatch, tmp_path)

    assert r["status"] == "degraded"
    assert r["index_recovery"]["motivo"] == "teste"
    assert r["index_recovery"]["fontes"] == "0/3"


def test_heartbeat_sem_reconstrucao_segue_saudavel(tmp_path, monkeypatch):
    r = _heartbeat(monkeypatch, tmp_path)
    assert r["status"] == "healthy"
    assert r["index_recovery"] is None
