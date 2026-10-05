"""compress pela rota local: o prompt referenciava `_lang`, apagado em 1d11649, e toda chamada falhava."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import delegation_core.server as server
from delegation_core.config import lang_instruction


def test_compress_local_chega_ao_modelo(monkeypatch):
    vistos = []

    class Motor:
        cfg = SimpleNamespace(is_cpu_budget=False, route=lambda **k: "local", synthesis_lang="pt")

        async def invoke(self, prompt, system="", **k):
            vistos.append((prompt, system))
            return "Decisao: migrar em outubro."

        def budget(self, task, requested=0):
            return requested

    async def direto(nome, fazer):
        return await fazer(Motor())

    monkeypatch.setattr(server, "_engine", Motor())
    monkeypatch.setattr(server, "_run_or_queue", direto)
    r = json.loads(asyncio.run(server.compress(source="email", raw_content="texto " * 200, use_local=True)))
    assert r.get("compressed") == "Decisao: migrar em outubro.", r
    assert vistos and "Source: email" in vistos[0][0]


def test_compress_local_leva_a_lingua_no_system(monkeypatch):
    """A lingua saiu do prompt e foi para o system; a correcao nao pode perde-la."""
    vistos = []
    cfg = SimpleNamespace(is_cpu_budget=False, route=lambda **k: "local", synthesis_lang="pt")

    class Motor:
        def __init__(self):
            self.cfg = cfg

        async def invoke(self, prompt, system="", **k):
            vistos.append(system)
            return "ok"

        def budget(self, task, requested=0):
            return requested

    async def direto(nome, fazer):
        return await fazer(Motor())

    monkeypatch.setattr(server, "_engine", Motor())
    monkeypatch.setattr(server, "_run_or_queue", direto)
    asyncio.run(server.compress(source="email", raw_content="texto " * 200, use_local=True))
    frase = lang_instruction(cfg)
    assert frase, "synthesis_lang=pt deveria ter frase de idioma"
    assert vistos and frase in vistos[0]
