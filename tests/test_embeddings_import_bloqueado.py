"""A falha de import do BGE tem de dizer a causa, e o heartbeat tem de ve-la.

Num Windows de 02/10/2026 o Smart App Control bloqueou
`torch\\_C.cp312-win_amd64.pyd`. O chromadb troca qualquer ImportError por
"The sentence_transformers python package is not installed", sem encadear a
causa, e o delegation-core repassava essa frase. O heartbeat seguia "healthy"
com a busca semantica fora do ar.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from delegation_core import embeddings
from delegation_core.config import Config

DLL = ("DLL load failed while importing _C: An Application Control policy "
       "has blocked this file.")


def test_dll_bloqueada_vira_erro_que_diz_a_causa(monkeypatch):
    def bloqueado():
        raise ImportError(DLL)
    monkeypatch.setattr(embeddings, "_importar_sentence_transformers", bloqueado)
    with pytest.raises(RuntimeError) as e:
        embeddings.make_bge_embedding_function("BAAI/bge-m3", device="cpu")
    msg = str(e.value)
    assert "Application Control" in msg
    assert "not installed" not in msg
    assert isinstance(e.value.__cause__, ImportError)


def test_falta_de_instalacao_de_verdade_continua_dita_como_falta():
    msg = embeddings.explicar_falha_de_import(ModuleNotFoundError("No module named 'torch'"))
    assert "No module named 'torch'" in msg
    assert "Application Control" not in msg


def _heartbeat(monkeypatch, tmp_path, erro):
    from delegation_core import server

    class Engine:
        cfg = Config(vault_path=str(tmp_path), engine_mode="agent")

    class Vault:
        init_error = erro

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


def test_heartbeat_sai_degradado_quando_o_bge_nao_subiu(tmp_path, monkeypatch):
    r = _heartbeat(monkeypatch, tmp_path, "PyTorch bloqueado: " + DLL)
    assert r["status"] == "degraded"
    assert r["embeddings"]["status"] == "unavailable"
    assert "Application Control" in r["embeddings"]["error"]


def test_heartbeat_com_bge_de_pe_nao_muda(tmp_path, monkeypatch):
    r = _heartbeat(monkeypatch, tmp_path, None)
    assert r["status"] == "healthy"
    assert r["embeddings"]["status"] == "ok"
