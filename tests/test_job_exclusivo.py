"""Duas tarefas que escrevem o vault inteiro nao podem rodar ao mesmo tempo.

## O defeito, medido em 26/09/2026

`jobs.submit` abria uma thread nova a cada chamada, sem olhar se ja existia um
job igual rodando. Nao havia dedupe em lugar nenhum do caminho.

Quem cria a concorrencia sao os hooks de sessao, e nao um uso exotico:

- o hook de fim de sessao dispara `delegation-core reindex` destacado
- o hook de inicio dispara `delegation-core maintain` destacado
- os dois delegam ao daemon, onde viram `vault_reindex_bg` e
  `run_maintenance_bg`

Fechar um terminal e abrir outro, ou fechar dois juntos, bastava para colocar
dois escritores do indice do vault em paralelo, fazendo o mesmo trabalho sobre o
mesmo dado.

## Por que a lista e curta, e por que `graph_build` fica fora

As quatro da lista nao sao particionadas por argumento: `vault_reindex` e
`run_maintenance` percorrem o vault inteiro, e as duas de ingestao escrevem o
mesmo registro. `graph_build` e `relink_folder` sao particionadas, um grafo ou
uma pasta por chamada, e duas chamadas diferentes sao trabalho diferente e
legitimo. Excluir mutuamente essas duas custaria paralelismo real sem evitar
corrida nenhuma.

## A regra que era humana

A instrucao "nunca rode duas ingestoes ao mesmo tempo" foi adotada em 31/08,
depois que duas corridas apagaram as entradas de Soteria e Pessoal do registro,
e sobreviveu quase um mes como instrucao. Uma defesa que depende de alguem
lembrar dela nao e defesa: o `conftest.py` desta suite existe pela mesma licao,
cobrada dois meses depois.
"""

from __future__ import annotations

import threading
import time

import pytest

from delegation_core import jobs


@pytest.fixture(autouse=True)
def registro_limpo():
    """Registro de jobs vazio por teste, esvaziado NO LUGAR.

    `_jobs` e estado de modulo, e um job vivo de outro teste faria a guarda de
    exclusividade absorver o pedido deste, ou o contrario, conforme a ordem.

    A primeira versao usava `monkeypatch.setattr(jobs, "_jobs", {})`, e as
    threads que ainda estavam terminando iam gravar o resultado no dicionario
    ANTIGO: `_worker` resolve `_jobs` como global na hora de gravar, entao cada
    teste deixava um `KeyError` solto. Trocar o conteudo em vez do objeto mantem
    a identidade que as threads ja capturaram.

    O teardown ainda espera as threads deste arquivo terminarem, porque limpar o
    registro debaixo de uma thread viva reintroduz o mesmo erro por outra porta.
    """
    anterior = dict(jobs._jobs)
    jobs._jobs.clear()
    yield
    limite = time.monotonic() + 5
    while jobs.running_count() and time.monotonic() < limite:
        time.sleep(0.01)
    jobs._jobs.clear()
    jobs._jobs.update(anterior)


def _tarefa_que_segura(evento):
    def fn():
        evento.wait(timeout=10)
        return "pronto"
    return fn


def test_segundo_pedido_da_mesma_tarefa_reaproveita_o_primeiro():
    """Conferido por mutacao: tirar a checagem de `TAREFAS_EXCLUSIVAS` do
    `submit` derruba este teste, e devolve os dois ids distintos."""
    solta = threading.Event()
    primeiro = jobs.submit("run_maintenance", _tarefa_que_segura(solta))
    segundo = jobs.submit("run_maintenance", _tarefa_que_segura(solta))

    assert primeiro == segundo, (
        "dois `run_maintenance` foram aceitos em paralelo")
    solta.set()


def test_o_modo_nao_separa_um_reindex_do_outro():
    """`vault_reindex: incremental` e `vault_reindex: full` sao a mesma tarefa.

    Escrevem o mesmo arquivo, entao comparar o nome inteiro deixaria correr
    justamente o par pior. Conferido por mutacao: comparar `j["task"]` cru em
    vez da familia derruba este teste.
    """
    solta = threading.Event()
    a = jobs.submit("vault_reindex: incremental", _tarefa_que_segura(solta))
    b = jobs.submit("vault_reindex: full", _tarefa_que_segura(solta))
    assert a == b, "um incremental e um full correram juntos"
    solta.set()


def test_o_pedido_absorvido_nao_fica_invisivel():
    """Reaproveitar sem dizer seria esconder do usuario que houve dois pedidos."""
    solta = threading.Event()
    jid = jobs.submit("ingest_folder", _tarefa_que_segura(solta))
    jobs.submit("ingest_folder", _tarefa_que_segura(solta))
    jobs.submit("ingest_folder", _tarefa_que_segura(solta))

    assert jobs.get(jid)["pedidos_coalescidos"] == 2
    solta.set()


def test_tarefa_terminada_nao_bloqueia_a_proxima():
    """A guarda e sobre o que esta RODANDO, nao sobre o historico.

    Sem isto, a primeira manutencao do dia bloquearia todas as seguintes para
    sempre, que seria muito pior que o defeito.
    """
    jid = jobs.submit("run_maintenance", lambda: "ok")
    for _ in range(100):
        if jobs.get(jid)["status"] == "done":
            break
        time.sleep(0.01)
    assert jobs.get(jid)["status"] == "done", "o job nao terminou; teste inconclusivo"

    novo = jobs.submit("run_maintenance", lambda: "ok")
    assert novo != jid, "a guarda passou a bloquear pelo historico"


@pytest.mark.parametrize("tarefa", ["graph_build", "relink_folder"])
def test_tarefa_particionada_continua_podendo_rodar_em_paralelo(tarefa):
    """A recusa deliberada, com o motivo no codigo.

    Um grafo por repositorio e uma pasta por chamada: duas chamadas diferentes
    sao trabalho diferente. Se alguem acrescentar estas a lista, este teste
    derruba e obriga a escrever por que.
    """
    solta = threading.Event()
    a = jobs.submit(tarefa, _tarefa_que_segura(solta))
    b = jobs.submit(tarefa, _tarefa_que_segura(solta))
    assert a != b, f"{tarefa} passou a ser exclusiva; era particionada de proposito"
    solta.set()


def test_a_lista_de_exclusivas_bate_com_tarefa_que_existe():
    """Nome morto na lista e protecao dada a ninguem.

    A mesma doenca que o `capabilities()` existe para conter: prosa que descreve
    um mundo que mudou. Le os nomes que o `server.py` realmente submete.
    """
    import inspect
    import re

    from delegation_core import server

    fonte = inspect.getsource(server)
    submetidos = {jobs._familia(m) for m in
                  re.findall(r'jobs\.submit\(\s*f?["\']([^"\']+)', fonte)}
    assert submetidos, "nenhum jobs.submit encontrado; este teste precisa de outro alvo"

    mortas = sorted(jobs.TAREFAS_EXCLUSIVAS - submetidos)
    assert not mortas, (
        f"exclusividade declarada para tarefa que ninguem submete: {mortas}")
