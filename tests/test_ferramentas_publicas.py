"""Nenhuma funcao interna publicada como ferramenta MCP.

`_bg_maintenance_wrapper` carregava `@mcp.tool()` e aparecia na lista servida
aos clientes: um agente podia chamar direto a manutencao inteira, fora do job
que da `job_id` e ritmo ao `task_status`. O prefixo `_` ja dizia que era
interna; o teste faz o servidor concordar.
"""

from __future__ import annotations

import asyncio

import delegation_core.server as server


def _nomes_publicados() -> list[str]:
    return [t.name for t in asyncio.run(server.mcp.list_tools())]


def test_a_lista_de_ferramentas_nao_esta_vazia():
    assert len(_nomes_publicados()) > 40


def test_nenhuma_ferramenta_publicada_tem_nome_interno():
    internas = [n for n in _nomes_publicados() if n.startswith("_")]
    assert not internas, f"funcao interna publicada como ferramenta MCP: {internas}"
