"""O registro de ingestao remontado a partir das linhas do indice.

Em 29/09/2026 um teste gravou por cima do `ingested_sources.json` real em duas
maquinas. Aqui: 23 fontes viraram 1. Num Mac: 172 viraram 1, e a reconstrucao
do indice de la pulou todas as fontes externas. O indice tinha a lista inteira,
em `source_folder` de cada linha externa. Estes testes usam o chromadb real
para escrever e o sqlite puro para ler, que e como o codigo le.
"""

import argparse
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from delegation_core import gpu, ingest, recuperacao
from delegation_core.config import Config
from delegation_core.vault import VaultManager


class _Embedder:
    def __call__(self, input):
        return [[1.0] + [0.0] * 7 for _ in input]

    def embed_documents(self, input):
        return self(input)

    def embed_query(self, input):
        return self([input])[0] if isinstance(input, str) else self(list(input))

    def name(self):
        return "probe"


@pytest.fixture
def indice(tmp_path, monkeypatch):
    """Um indice real com duas fontes externas e uma nota do vault."""
    monkeypatch.setattr(gpu, "take", lambda *a, **k: None)
    cfg = Config(vault_path=str(tmp_path / "vault"), vault_folders=["Notes"],
                 search_threshold=0.0)
    (tmp_path / "vault" / "Notes").mkdir(parents=True)
    fonte_a, fonte_b = tmp_path / "docs_a", tmp_path / "docs_b"
    fonte_a.mkdir()
    fonte_b.mkdir()
    parado = fonte_a / "parado.md"
    mudou = fonte_a / "mudou.md"
    outro = fonte_b / "outro.md"
    for f in (parado, mudou, outro):
        f.write_text(f"conteudo de {f.name}")
    # Ingerido "agora", depois do mtime de todos.
    agora = (datetime.now() + timedelta(seconds=5)).isoformat()

    vm = VaultManager(cfg)
    vm.ef = _Embedder()
    vm._init()
    for fonte, arquivo in ((fonte_a, parado), (fonte_a, mudou), (fonte_b, outro)):
        vm.index_note(arquivo.read_text(), {
            "title": arquivo.stem, "path": str(arquivo), "folder": "_external",
            "source_folder": str(fonte), "ingested_at": agora,
            "is_external": "true", "chunk": "0", "total_chunks": "1"},
            doc_id=str(arquivo))
    vm.index_note("nota do vault", vm.note_metadata("Notes/n.md", "n", "Notes", "x"))
    vm._close_client()
    # `mudou` e alterado depois da ingestao.
    futuro = time.time() + 3600
    os.utime(mudou, (futuro, futuro))
    return cfg, fonte_a, fonte_b, parado, mudou, outro


def test_le_as_fontes_do_indice_sem_o_chromadb(indice):
    cfg, fonte_a, fonte_b, parado, mudou, outro = indice

    fontes = ingest.fontes_do_indice(cfg.chroma_path)

    assert set(fontes) == {str(fonte_a), str(fonte_b)}
    assert set(fontes[str(fonte_a)]) == {str(parado), str(mudou)}


def test_indice_ausente_nao_tem_fontes(tmp_path):
    assert ingest.fontes_do_indice(tmp_path / "nada") == {}


def test_reconstroi_o_registro_e_carimba_so_o_que_nao_mudou(indice):
    """O caso desta maquina: registro destruido, indice saudavel."""
    cfg, fonte_a, fonte_b, parado, mudou, outro = indice
    ingest._save_registry({"/tmp/pytest-of-x/docs": {"files": {}}})

    registro = ingest.reconstruir_registro_do_indice(cfg.chroma_path, carimbar_arquivos=True)

    assert {str(fonte_a), str(fonte_b)} <= set(registro)
    carimbos = registro[str(fonte_a)]["files"]
    assert str(parado) in carimbos, "arquivo que nao mudou tem que ser carimbado"
    assert str(mudou) not in carimbos, "arquivo que mudou tem que ser reembutido"
    assert registro[str(fonte_a)]["recursive"] is True


def test_entrada_existente_mantem_as_configuracoes(indice):
    cfg, fonte_a, *_ = indice
    ingest._save_registry({str(fonte_a): {"recursive": False, "exclude": ["*.log"], "files": {}}})

    registro = ingest.reconstruir_registro_do_indice(cfg.chroma_path, carimbar_arquivos=False)

    assert registro[str(fonte_a)]["recursive"] is False
    assert registro[str(fonte_a)]["exclude"] == ["*.log"]
    assert registro[str(fonte_a)]["files"] == {}


def test_quarentena_com_registro_errado_recupera_as_fontes_do_indice(indice):
    """O Mac de 29/09: registro reduzido a uma fonte falsa do pytest, e a
    reconstrucao enfileirou so ela. A lista tem que vir do indice tambem."""
    cfg, fonte_a, fonte_b, *_ = indice
    falsa = "/private/var/folders/xx/pytest-1/test_quarentena/docs"
    ingest._save_registry({falsa: {"files": {}}})

    pedido = recuperacao.pos_em_quarentena(cfg, motivo="teste")

    caminhos = {f["path"] for f in pedido["fontes"]}
    assert {str(fonte_a), str(fonte_b), falsa} == caminhos
    assert all(not (e.get("files")) for e in ingest._load_registry().values()), \
        "as linhas do indice em quarentena nao estao no novo: nenhum carimbo vale"


# ── cli ──────────────────────────────────────────────────────────────────────

def _args(**kw):
    base = {"from_index": "", "queue": False}
    base.update(kw)
    return argparse.Namespace(**base)


def test_cli_sem_from_remonta_do_indice_em_uso(indice, monkeypatch):
    from delegation_core import cli
    cfg, fonte_a, *_ = indice
    monkeypatch.setattr(cli, "_graph_config", lambda: cfg)

    cli.cmd_ingest_registry(_args())

    assert str(fonte_a) in ingest._load_registry()
    assert recuperacao.reconstrucao_pendente() is None


def test_cli_queue_poe_as_fontes_da_quarentena_na_fila(indice, monkeypatch, tmp_path):
    from delegation_core import cli, daemon
    cfg, fonte_a, fonte_b, *_ = indice
    quarentena = cfg.chroma_path.with_name(".chroma_bge-danificado-teste")
    os.rename(cfg.chroma_path, quarentena)
    # Um carimbo que sobrou de antes: certifica uma linha que o indice novo nao
    # tem, e deixaria o `ingest` da reconstrucao pular o arquivo.
    ingest._save_registry({str(fonte_a): {"recursive": True, "files": {"x.md": [1.0, 1]}}})
    monkeypatch.setattr(cli, "_graph_config", lambda: cfg)
    monkeypatch.setattr(daemon, "is_listening", lambda _c: False)

    cli.cmd_ingest_registry(_args(from_index=str(quarentena), queue=True))

    pedido = recuperacao.reconstrucao_pendente()
    assert pedido["notas_feitas"] is True, "as notas ja foram refeitas; so as fontes faltam"
    assert {f["path"] for f in pedido["fontes"]} == {str(fonte_a), str(fonte_b)}
    assert all(not e.get("files") for e in ingest._load_registry().values())


def test_cli_queue_soma_ao_pedido_em_andamento(indice, monkeypatch):
    from delegation_core import cli, daemon
    cfg, fonte_a, fonte_b, *_ = indice
    quarentena = cfg.chroma_path.with_name(".chroma_bge-danificado-teste")
    os.rename(cfg.chroma_path, quarentena)
    recuperacao._gravar_json(recuperacao.caminho_do_pedido(), {
        "notas_feitas": False, "fontes": [{"path": "/falsa", "recursive": True, "exclude": []}],
        "fontes_feitas": []})
    monkeypatch.setattr(cli, "_graph_config", lambda: cfg)
    monkeypatch.setattr(daemon, "is_listening", lambda _c: False)

    cli.cmd_ingest_registry(_args(from_index=str(quarentena), queue=True))

    pedido = recuperacao.reconstrucao_pendente()
    assert pedido["notas_feitas"] is False, "o pedido em andamento nao pode perder a fase das notas"
    assert [f["path"] for f in pedido["fontes"]][0] == "/falsa"
    assert {str(fonte_a), str(fonte_b)} <= {f["path"] for f in pedido["fontes"]}


def test_cli_queue_recusa_o_indice_em_uso(indice, monkeypatch):
    from delegation_core import cli
    cfg, *_ = indice
    monkeypatch.setattr(cli, "_graph_config", lambda: cfg)

    with pytest.raises(SystemExit):
        cli.cmd_ingest_registry(_args(queue=True))


def test_cli_queue_recusa_com_o_daemon_no_ar(indice, monkeypatch):
    from delegation_core import cli, daemon
    cfg, *_ = indice
    quarentena = cfg.chroma_path.with_name(".chroma_bge-danificado-teste")
    os.rename(cfg.chroma_path, quarentena)
    monkeypatch.setattr(cli, "_graph_config", lambda: cfg)
    monkeypatch.setattr(daemon, "is_listening", lambda _c: True)

    with pytest.raises(SystemExit):
        cli.cmd_ingest_registry(_args(from_index=str(quarentena), queue=True))
    assert recuperacao.reconstrucao_pendente() is None
