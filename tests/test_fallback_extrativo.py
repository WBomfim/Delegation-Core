"""O modo agente devolvia o PROMPT como se fosse a resposta, e isso foi para o vault.

`DelegationEngine.invoke` curto-circuita em modo agente e chama
`_extractive_fallback`, cujo docstring declara um contrato:

    Callers format prompts as "<instruction>\\n\\n<raw payload>", so we drop the
    leading instruction line and return the payload truncated to roughly
    max_tokens*4 characters. This is not a summary - it's a safe pass-through.

O contrato e FALSO para o codigo em que ele vive. Conferido chamador por
chamador, com o AST reconstruindo cada prompt:

    synthesizer.py:386      tem \\n\\n, mas a carga esta no FIM do gabarito.
                            Cortar no primeiro corta entre duas INSTRUCOES.
    organizer.py:584        NAO tem \\n\\n. O prompt inteiro volta.
    organizer.py:120        NAO tem \\n\\n, e a ordem e invertida: conteudo
                            primeiro, instrucao depois.
    classifier.py:75        tem \\n\\n e a carga vem depois. O unico que casa.
    localworker.py:146      passa force_local=True e nao chega aqui.

## O QUE ISSO GRAVOU NO VAULT, medido em 04/09/2026

8.596 notas varridas, TRES com texto de prompt no corpo:

    Reference/2026-09-03-Relatorio do job noturno - defeitos silenciosos.md
    Reference/2026-09-03-Validacao tecnica dos Agentes PMO - o que o cliente.md
    2026-W35-maintenance.md

As duas primeiras comecam com "OUTPUT: Markdown only. No preamble, no trailing
explanation..." -- byte a byte o segundo paragrafo de `_EN_DOC_PROMPT`, que e o
que sobra depois do corte no primeiro \\n\\n. A terceira comeca com "Write a
3-sentence vault maintenance summary." inteirinha, porque aquele prompt nao tem
\\n\\n nenhum.

Duas delas carregam `quality_issues: ["prompt_leak"]` no frontmatter: o detector
via o SINTOMA e ninguem tinha rastreado a causa. A terceira escapa ate do
detector, porque nao passa pela pontuacao de qualidade.

## A CORRECAO

Adivinhar onde a instrucao acaba e o defeito. O chamador SABE qual e a carga, e
passa a dizer, em `fallback_payload`. Quem nao disser continua caindo na
heuristica, e agora com um aviso no log nomeando o `task`, porque um chamador
novo que esqueca merece falhar alto e nao silenciosamente escrever prompt no
vault do usuario.
"""
from __future__ import annotations

import logging

import pytest

from delegation_core.config import Config
from delegation_core.engine import DelegationEngine


def _motor(**kw) -> DelegationEngine:
    cfg = Config(vault_path="/tmp/x", engine_mode="agent", **kw)
    return DelegationEngine(cfg)


def _rodar(coro):
    import asyncio
    return asyncio.run(coro)


# ── o contrato explicito ────────────────────────────────────────────────────


def test_a_carga_declarada_e_o_que_volta():
    motor = _motor()
    saida = _rodar(motor.invoke("Instrucao qualquer.\nSem linha em branco.",
                                fallback_payload="ESTE E O CONTEUDO DE VERDADE"))
    assert saida == "ESTE E O CONTEUDO DE VERDADE"


def test_a_carga_declarada_vence_a_heuristica():
    """Mesmo quando o prompt TEM \\n\\n, o chamador manda."""
    motor = _motor()
    saida = _rodar(motor.invoke("Instrucao.\n\nParte que a heuristica devolveria",
                                fallback_payload="a carga de verdade"))
    assert saida == "a carga de verdade"


def test_a_carga_declarada_respeita_o_teto_de_caracteres():
    motor = _motor()
    saida = _rodar(motor.invoke("x", fallback_payload="A" * 5000, max_tokens=10))
    assert len(saida) <= 5000
    assert saida.startswith("A")


def test_sem_carga_declarada_ainda_funciona_mas_avisa(caplog):
    """A heuristica fica como ultimo recurso. O que nao pode e ser silenciosa:
    era assim que prompt virava nota."""
    motor = _motor()
    with caplog.at_level(logging.WARNING):
        _rodar(motor.invoke("Instrucao.\n\ncarga", task="algum_task"))

    assert any("fallback_payload" in r.message or "algum_task" in r.message
               for r in caplog.records), "um chamador que esquece tem que aparecer no log"


# ── os chamadores reais, um por um ──────────────────────────────────────────


def test_o_resumo_de_manutencao_nao_e_mais_o_proprio_prompt():
    """O caso gravado em 2026-W35-maintenance.md, na raiz do vault."""
    import asyncio

    from delegation_core import organizer

    cfg = Config(vault_path="/tmp/x", engine_mode="agent")
    motor = DelegationEngine(cfg)
    resultados = {"classified": ["a.md -> Reference/a.md", "b.md -> Fixes/b.md"],
                  "merged": [], "errors": [], "junk": ["LICENSE"]}

    corpo = asyncio.run(organizer._resumo_de_manutencao(motor, resultados))

    assert "Write a 3-sentence vault maintenance summary" not in corpo
    assert "2" in corpo, "o numero de notas classificadas tem que aparecer"


def test_o_titulo_de_secao_nao_vira_o_prompt():
    """`_upgrade_one` substitui um titulo-marcador. Devolver o prompt poria
    'Content summary: ... Write a 3-5 word title' como CABECALHO da nota."""
    from delegation_core import organizer

    cfg = Config(vault_path="/tmp/x", engine_mode="agent")
    motor = DelegationEngine(cfg)

    novo = _rodar(organizer._titulo_de_secao(motor, "Section 1", "conteudo qualquer"))

    assert "Write a 3-5 word title" not in novo
    assert "Content summary" not in novo


def test_a_sintese_devolve_o_texto_fonte_e_nao_o_gabarito():
    """"Safe pass-through" era a intencao declarada. O que passava era o
    gabarito, nao a fonte."""
    from delegation_core import synthesizer

    cfg = Config(vault_path="/tmp/x", engine_mode="agent", synthesis_lang="en")
    motor = DelegationEngine(cfg)

    saida = _rodar(synthesizer.synthesize(
        motor, sidecar={}, content="O TEXTO ORIGINAL DO DOCUMENTO",
        filename="doc.pdf", fmt="pdf", today="2026-09-04"))

    texto = saida if isinstance(saida, str) else str(saida)
    assert "OUTPUT: Markdown only" not in texto
    assert "FORMAT (follow literally" not in texto


def test_o_classificador_continua_devolvendo_pasta_valida():
    """Ele ja era seguro, porque valida a resposta contra a lista de pastas e cai
    num fallback quando nao casa. Pinado para nao regredir: o fallback do modo
    agente pode devolver qualquer coisa e isto tem que continuar sendo uma pasta
    de verdade."""
    import asyncio

    from delegation_core.classifier import classify

    cfg = Config(vault_path="/tmp/x", engine_mode="agent",
                 vault_folders=["Reference", "Fixes"])
    motor = DelegationEngine(cfg)

    pasta = asyncio.run(classify(motor, ["Reference", "Fixes"],
                                 "arq.md", "conteudo qualquer", "md"))

    assert pasta in ("Reference", "Fixes")


# ── e o destino do resumo, que caia na raiz do vault ────────────────────────


def test_o_resumo_semanal_vai_para_a_pasta_de_sessoes_com_a_caixa_do_vault(tmp_path):
    """`(cfg.vault / "sessions").exists()` e falso num vault cuja pasta e
    "Sessions", entao o resumo caia na RAIZ.

    Consequencia medida em 04/09/2026: `<vault>/2026-W35-maintenance.md` existia,
    fora de toda pasta configurada. `reindex_vault` caminha por cfg.vault_folders,
    entao a nota nunca foi indexada -- conferido, nenhuma chave sem barra em
    .chroma_index.json -- e `get_health_summary` nunca a graduou.
    """
    import asyncio

    from delegation_core import organizer

    (tmp_path / "Sessions").mkdir()
    cfg = Config(vault_path=str(tmp_path), vault_folders=["Reference", "Sessions"],
                 engine_mode="agent")
    motor = DelegationEngine(cfg)

    asyncio.run(organizer._write_summary(motor, cfg, {"classified": ["a.md"]}))

    na_pasta = list((tmp_path / "Sessions").glob("*-maintenance.md"))
    na_raiz = list(tmp_path.glob("*-maintenance.md"))
    assert na_pasta, "o resumo nao foi para a pasta de sessoes"
    assert not na_raiz, f"o resumo caiu na raiz do vault: {na_raiz}"


def test_sem_pasta_de_sessoes_configurada_ainda_escreve_em_algum_lugar(tmp_path):
    """Um vault que nao configura sessoes nao pode perder o resumo."""
    import asyncio

    from delegation_core import organizer

    cfg = Config(vault_path=str(tmp_path), vault_folders=["Reference"],
                 engine_mode="agent")
    motor = DelegationEngine(cfg)

    asyncio.run(organizer._write_summary(motor, cfg, {"classified": []}))

    assert list(tmp_path.rglob("*-maintenance.md"))


def test_o_corpo_do_resumo_no_disco_nao_e_o_prompt(tmp_path):
    """As duas correcoes juntas, no arquivo que fica de verdade."""
    import asyncio

    from delegation_core import organizer

    (tmp_path / "Sessions").mkdir()
    cfg = Config(vault_path=str(tmp_path), vault_folders=["Sessions"],
                 engine_mode="agent")
    motor = DelegationEngine(cfg)

    asyncio.run(organizer._write_summary(motor, cfg,
                                         {"classified": ["a.md", "b.md"], "merged": []}))

    texto = next((tmp_path / "Sessions").glob("*-maintenance.md")).read_text(encoding="utf-8")
    assert "Write a 3-sentence vault maintenance summary" not in texto
    assert "Classified: a.md" not in texto, "os dados crus do prompt vazaram"
