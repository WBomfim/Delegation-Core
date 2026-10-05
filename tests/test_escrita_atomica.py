"""Estado persistente escrito por cima do arquivo real.

`write_text` TRUNCA o arquivo antes de escrever. Se o processo morre no meio, ou
o disco enche, o que fica no disco e um arquivo pela metade, indistinguivel de
um corrompido.

Este projeto ja decidiu que isso importa, SEIS VEZES, com o motivo escrito em
cada uma:

    localqueue._write   "Atomic replace: the worker and the MCP tools write this
                         file from different threads, and a half-written store
                         is indistinguishable from a corrupt one."
    jobs._record_duration, tracker, windows, ingest, clients: o mesmo padrao
                         tmp + fsync + replace.

E ficaram de fora os dois arquivos cuja perda custa mais:

**config.json** e a razao de ser da maquina. O `conftest.py` desta suite existe
por causa dele: em 02/09 um teste chamou `calibrate()`, o `cfg.save()` no fim
sobrescreveu a config real, o daemon subiu sem vault, o systemd desistiu depois
de cinco tentativas e a maquina ficou sem servico ate alguem reescrever o
arquivo a mao. `Config.load()` num arquivo ilegivel devolve `cls()`, ou seja
DEFAULTS, e um default sem vault_path e exatamente "not configured".

**.chroma_index.json** tem 634.449 bytes e 8.594 carimbos neste vault, medido em
03/09/2026. `_load_index_state` responde `{}` a qualquer falha de leitura, e um
`{}` faz TODA nota parecer nao carimbada: o proximo reindex "incremental"
reembute o vault inteiro. O historico desta maquina tem um reindex de 642,3s.

HONESTIDADE SOBRE A EVIDENCIA: nenhuma escrita rasgada foi observada aqui. O
`config.json.QUEBRADO-20260902-221253` que existe nesta maquina e bem formado --
e o artefato do incidente do `calibrate()`, conteudo errado e nao escrita
partida. O argumento nao e um caso observado, e a inconsistencia: o projeto
decidiu seis vezes que este modo de falha importa e deixou de fora o arquivo
cuja perda ja tirou a maquina do ar por outro caminho.
"""
from __future__ import annotations

import json
import os

import pytest

from delegation_core.config import Config
from delegation_core.vault import VaultManager


def _escrita_rasgada(monkeypatch, modulo):
    """Modela o disco enchendo NO MEIO da escrita, nos dois estilos de codigo.

    Duas armadilhas custaram uma versao cada:

    1. A primeira fazia `json.dumps` explodir, e passava sem provar nada:
       `dumps` roda ANTES de qualquer abertura de arquivo, entao o original
       nunca chegava a ser truncado.
    2. A segunda so remendava `Path.write_text`, que a implementacao ATOMICA
       nao usa: o dublê nunca disparava, nada falhava, e o `pytest.raises`
       falhava sozinho. Um dublê que so conhece o codigo velho testa o codigo
       velho.

    Entao aqui as duas portas falham: `write_text` no destino (o estilo antigo,
    truncando antes de estourar) e `os.fsync` (o estilo atomico, depois do tmp
    escrito e ANTES do replace). Qualquer das duas implementacoes falha; so a
    atomica deixa o arquivo de destino intacto.
    """
    from pathlib import Path as _Path
    real = _Path.write_text

    def _rasga(self, dados, *a, **k):
        if self.name.endswith((".json", ".json.tmp")):
            self.write_bytes(b"")          # o truncamento que write_text faz
            raise OSError("disco cheio no meio da escrita")
        return real(self, dados, *a, **k)

    def _fsync_falha(fd):
        raise OSError("disco cheio no flush")

    monkeypatch.setattr(_Path, "write_text", _rasga)
    monkeypatch.setattr(modulo.os, "fsync", _fsync_falha)


@pytest.fixture
def dir_limpo(tmp_path):
    """Subdiretorio proprio: o conftest ja povoa tmp_path com o CONFIG_DIR
    falso e o diretorio de units, e um teste que lista tmp_path os encontra."""
    d = tmp_path / "so-deste-teste"
    d.mkdir()
    return d


# ── config.json ─────────────────────────────────────────────────────────────


def test_uma_escrita_que_falha_no_meio_nao_destroi_a_config_anterior(dir_limpo, monkeypatch):
    """So passa com tmp+replace. Com write_text direto no destino, o arquivo
    original ja esta vazio quando a falha acontece."""
    import delegation_core.config as config_mod

    destino = dir_limpo / "config.json"
    destino.write_text(json.dumps({"vault_path": "/o/vault/de/verdade"}), encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_DIR", dir_limpo)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", destino)

    _escrita_rasgada(monkeypatch, config_mod)
    with pytest.raises(OSError):
        Config(vault_path="/novo").save()

    sobreviveu = json.loads(destino.read_text(encoding="utf-8"))
    assert sobreviveu["vault_path"] == "/o/vault/de/verdade"


def test_a_config_salva_continua_legivel_e_completa(dir_limpo, monkeypatch):
    import delegation_core.config as config_mod

    destino = dir_limpo / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", dir_limpo)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", destino)

    Config(vault_path="/um/vault", server_token="segredo").save()

    lido = json.loads(destino.read_text(encoding="utf-8"))
    assert lido["vault_path"] == "/um/vault"
    assert lido["server_token"] == "segredo"


@pytest.mark.skipif(os.name == "nt", reason="modo de arquivo POSIX")
def test_o_token_nunca_fica_legivel_por_terceiros_nem_por_um_instante(dir_limpo, monkeypatch):
    """A ordem antiga era escrever e DEPOIS chmod 600, o que deixa uma janela
    com o token em 0644. Escrevendo no tmp com 0600 antes do replace, a janela
    nao existe."""
    import delegation_core.config as config_mod

    destino = dir_limpo / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", dir_limpo)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", destino)

    modos = []
    real_replace = os.replace

    def _espia(src, dst):
        modos.append(oct(os.stat(src).st_mode & 0o777))
        return real_replace(src, dst)

    monkeypatch.setattr(config_mod.os, "replace", _espia)
    Config(vault_path="/v", server_token="segredo").save()

    assert modos == ["0o600"], f"o arquivo temporario passou por {modos}"
    assert oct(destino.stat().st_mode & 0o777) == "0o600"


def test_nao_deixa_arquivo_temporario_para_tras(dir_limpo, monkeypatch):
    import delegation_core.config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_DIR", dir_limpo)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", dir_limpo / "config.json")

    Config(vault_path="/v").save()

    restos = [p.name for p in dir_limpo.iterdir() if p.name != "config.json"]
    assert restos == []


# ── .chroma_index.json ──────────────────────────────────────────────────────


def _vm(tmp_path):
    cfg = Config(vault_path=str(tmp_path))
    v = VaultManager(cfg)
    v._ensure_ready = lambda: None
    v.collection = None
    return v


def test_o_estado_do_indice_sobrevive_a_uma_escrita_que_falha(tmp_path, monkeypatch):
    v = _vm(tmp_path)
    v._save_index_state({"a.md": 1.0, "b.md": 2.0})
    antes = v._load_index_state()

    # A escrita atomica do estado mora no notes (gravar_estado); e o os de la
    # que precisa falhar.
    import delegation_core.notes as notes_mod
    _escrita_rasgada(monkeypatch, notes_mod)
    v._save_index_state({"c.md": 3.0})     # engolida por design, mas nao pode destruir

    assert v._load_index_state() == antes, (
        "8.594 carimbos perdidos fazem o proximo reindex incremental reembutir "
        "o vault inteiro"
    )


def test_estado_ilegivel_avisa_em_vez_de_ficar_calado(tmp_path, caplog):
    """`except Exception: pass` era mudo. O sintoma e um reindex 'incremental'
    de dez minutos sem nenhuma explicacao em lugar nenhum."""
    import logging

    v = _vm(tmp_path)
    v._index_state_path().write_text("{ isto nao e json", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        assert v._load_index_state() == {}

    assert any("index state" in r.message.lower() or "estado" in r.message.lower()
               for r in caplog.records), "a perda dos carimbos tem que aparecer no log"


def test_o_estado_do_indice_continua_indo_e_voltando(tmp_path):
    v = _vm(tmp_path)
    v._save_index_state({"Sessions/a.md": 123.5})
    assert v._load_index_state() == {"Sessions/a.md": 123.5}
