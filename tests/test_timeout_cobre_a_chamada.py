"""O `timeout` de `submit_and_wait` tem que cobrir a chamada, nao so o polling.

## O defeito, medido em 26/09/2026

`submit_and_wait(..., timeout=X)` construia o cliente HTTP com a constante
`CALL_TIMEOUT_SEC` (120 s) e passava `X` apenas para o laco que consulta
`task_status`. Para uma ferramenta **assincrona** isso quase nao aparece, porque
o submit e barato e a espera longa acontece no laco. Para uma ferramenta
**sincrona** o laco nunca roda: `X` governava nada, e o teto real continuava 120 s.

Medido num lote de renomeacao de 70 notas:

| | |
|---|---|
| custo de um `vault_rename_note` neste vault | 64 a 125 s |
| teto que o chamador acreditava ter pedido | 600 s |
| teto real | ~120 s |
| resultado | as que passavam de 120 s falhavam, cerca de metade |

O custo alto e legitimo e nao e o defeito: o rename repõe os wikilinks de toda
nota que apontava para a renomeada e re-embute cada uma, com o encoder na CPU
desde a divisao de placa daquele dia. O defeito e um parametro chamado `timeout`
que nao cobre a operacao que o chamador esta cronometrando.

## Por que o piso permanece

Pedir menos que o padrao nao pode deixar uma chamada comum mais fragil do que
era, entao o teto e `max(CALL_TIMEOUT_SEC, timeout)`. E `timeout=None` significa
"espere o quanto for" para o laco, que nao da numero para um cliente HTTP: nesse
caso fica o padrao, em vez de infinito.
"""

from __future__ import annotations

import pytest

from delegation_core import daemon


@pytest.fixture
def cfg():
    from delegation_core.config import Config
    return Config(server_host="127.0.0.1", server_port=8787)


@pytest.fixture
def espiao(monkeypatch):
    """Captura o teto com que o cliente HTTP foi construido.

    Nao ha daemon no teste: o cliente falso responde um payload sincrono, sem
    `job_id`, que e exatamente a forma que nunca chegava ao laco de polling.
    """
    vistos: list[float] = []

    class ClienteFalso:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def call_tool(self, tool, arguments):
            return {"status": "ok", "tool": tool}

    def build(cfg, timeout):
        vistos.append(timeout)
        return ClienteFalso()

    monkeypatch.setattr(daemon, "_build_client", build)
    monkeypatch.setattr(daemon, "is_listening", lambda cfg: True)
    monkeypatch.setattr(daemon, "_payload", lambda r: r)
    return vistos


def test_timeout_grande_levanta_o_teto_da_chamada(cfg, espiao):
    """O caso real: ferramenta sincrona, teto pedido de 600 s.

    Conferido por mutacao: voltar `_build_client(cfg, CALL_TIMEOUT_SEC)` derruba
    este teste, e no sistema real derruba metade de um lote de renomeacao.
    """
    daemon.submit_and_wait(cfg, "vault_rename_note", {"path": "x", "new_title": "y"},
                           timeout=600)
    assert espiao == [600.0], (
        f"o cliente foi construido com {espiao}, e nao com os 600 s pedidos")


def test_pedir_menos_que_o_padrao_nao_encurta_o_teto(cfg, espiao):
    """O piso. Um chamador apressado nao pode tornar a chamada mais fragil."""
    daemon.submit_and_wait(cfg, "heartbeat", {}, timeout=5)
    assert espiao == [daemon.CALL_TIMEOUT_SEC]


def test_espera_ilimitada_nao_passa_infinito_para_o_cliente(cfg, espiao):
    """`timeout=None` e `timeout=0` significam "o quanto for" para o LACO.

    Um cliente HTTP nao aceita isso como numero, entao a chamada fica no padrao.
    Sem esta distincao o `_build_client` receberia `inf`.
    """
    for pedido in (None, 0):
        espiao.clear()
        daemon.submit_and_wait(cfg, "heartbeat", {}, timeout=pedido)
        assert espiao == [daemon.CALL_TIMEOUT_SEC], f"timeout={pedido} -> {espiao}"
        assert espiao[0] != float("inf")


def test_o_padrao_de_espera_de_job_continua_valendo(cfg, espiao):
    """`JOB_WAIT_TIMEOUT_SEC` e o padrao do parametro, e e maior que o teto de
    chamada, entao um chamador que nao passa nada passa a ganhar o teto grande.

    Isso e deliberado e vale dizer em teste: quem espera uma hora por um job
    aceita esperar uma hora pelo submit dele.
    """
    daemon.submit_and_wait(cfg, "heartbeat", {})
    assert espiao == [max(daemon.CALL_TIMEOUT_SEC, daemon.JOB_WAIT_TIMEOUT_SEC)]


def test_o_docstring_nao_volta_a_omitir_o_que_o_timeout_cobre():
    """A prosa faz parte da correcao aqui.

    O defeito custou meio lote justamente porque a assinatura parecia dizer uma
    coisa e o codigo fazia outra. Se alguem reescrever o docstring sem isso, a
    proxima pessoa cai igual.
    """
    # Espaco normalizado: o docstring e quebrado em linhas, e a primeira versao
    # deste teste procurou "tool call itself" cru e falhou porque a frase cai
    # entre duas linhas. Um teste que depende de onde o texto quebra guarda a
    # formatacao, nao a afirmacao.
    doc = " ".join((daemon.submit_and_wait.__doc__ or "").split())
    assert "includes the tool call itself" in doc, (
        "o docstring deixou de dizer que o timeout cobre a propria chamada")
    assert "SYNCHRONOUS tool never reaches the polling loop" in doc, (
        "o docstring deixou de dizer POR QUE isso importa para ferramenta sincrona")
