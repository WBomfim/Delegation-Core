"""Numeros que a prosa promete e o codigo desmente.

`test_version_consistency.py` ja resolveu esta familia para `__version__`: a
versao drifou tres vezes, foi re-sincronizada a mao tres vezes com um comentario
prometendo lockstep, e so parou quando um teste passou a exigir que a COPIA nao
existisse. Este arquivo faz o mesmo com as contagens.

Medido em 02/09/2026 contra a arvore de trabalho:

  HANDOFF.md dizia  "204 passing", "19 test files"   -> 1042 testes em 67 arquivos
  HANDOFF.md dizia  "31 public MCP tools"            -> 54 registros @mcp.tool()
  README.md  dizia  "579 tests"                      -> 1042
  AGENT_GUIDE dizia tool_count 45 num exemplo        -> 54

O HANDOFF abre com "Facts below were true and verified at the timestamp above",
e a data era 28/07. Cinco semanas de deriva num documento escrito para ser a
primeira coisa que o proximo agente le.

A correcao nao e sincronizar as copias, e tirar as copias volateis e guardar as
que importam. Um total de testes muda a cada teste escrito: nao pertence a
prosa. O numero de ferramentas MCP e superficie de API, muda raramente, e alguem
que le "31" e planeja em cima disso planeja errado: esse fica, e e verificado.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
HANDOFF = RAIZ / "HANDOFF.md"
README = RAIZ / "README.md"
SERVER = RAIZ / "src" / "delegation_core" / "server.py"

#: "204 passing", "579 tests", "1042 unit tests", "19 test files".
_CONTAGEM_DE_TESTES = re.compile(
    r"\b\d{2,5}\s+(?:unit\s+)?(?:tests?\b|passing\b|test\s+files\b)", re.IGNORECASE)


def _secao(texto: str, titulo: str) -> str:
    """O corpo de uma secao markdown, ate o proximo cabecalho de mesmo nivel."""
    linhas = texto.split("\n")
    try:
        inicio = next(i for i, ln in enumerate(linhas) if ln.strip() == titulo)
    except StopIteration:
        pytest.fail(f"secao {titulo!r} sumiu de {HANDOFF.name}")
    nivel = titulo.split(" ")[0]
    for j in range(inicio + 1, len(linhas)):
        if linhas[j].startswith(nivel + " "):
            return "\n".join(linhas[inicio:j])
    return "\n".join(linhas[inicio:])


def test_handoff_nao_promete_um_total_de_testes():
    """A secao que se declara verificada nao pode carregar um numero que apodrece.

    Historico datado ("Recent history (Session Summary - 2026-07-28)") fica de
    fora de proposito: ali um numero antigo e o registro do que era verdade
    naquele dia, nao uma afirmacao sobre agora.
    """
    atual = _secao(HANDOFF.read_text(encoding="utf-8"), "## Current state (all verified, not assumed)")
    # A propria explicacao cita as contagens antigas entre aspas. Ler o que esta
    # citado como se fosse afirmacao seria o teste falhando pelo motivo errado.
    sem_citacoes = re.sub(r'"[^"]*"', "", atual)

    achados = _CONTAGEM_DE_TESTES.findall(sem_citacoes)
    assert not achados, (
        f"HANDOFF.md voltou a fixar contagem de testes: {achados}. "
        "Rode a suite; nao escreva o total. Ele drifou de 204 para 1042 "
        "sem que nada falhasse."
    )


def test_readme_nao_promete_um_total_de_testes():
    texto = README.read_text(encoding="utf-8")
    sem_citacoes = re.sub(r'"[^"]*"', "", texto)
    achados = _CONTAGEM_DE_TESTES.findall(sem_citacoes)
    assert not achados, (
        f"README.md voltou a fixar contagem de testes: {achados}. "
        "Chegou a dizer 579 contra 1042 reais."
    )


def test_contagem_de_ferramentas_mcp_na_prosa_bate_com_o_servidor():
    """Superficie de API: esta pode ficar na prosa, entao tem que estar certa.

    Quem le "31 public MCP tools" e decide o que cabe no contexto do cliente
    decide com um numero 43% menor que o real.
    """
    real = len(re.findall(r"^@mcp\.tool\(\)", SERVER.read_text(encoding="utf-8"), re.MULTILINE))
    assert real > 0, "nenhum @mcp.tool() encontrado — o padrao de busca quebrou"

    # docs/ entrou depois que o docs/MAPA.md ficou dizendo 54 com o servidor em
    # 56, sem que nada falhasse: o teste so olhava HANDOFF e README.
    docs = sorted((HANDOFF.parent / "docs").glob("*.md"))
    assert docs, "nenhum .md em docs/: o caminho do teste quebrou"
    for doc in (HANDOFF, README, *docs):
        texto = doc.read_text(encoding="utf-8")
        for achado in re.finditer(r"\b(\d{1,4})\s+(?:public\s+)?(?:MCP\s+tools|ferramentas\s+MCP|`?@mcp\.tool)", texto):
            assert int(achado.group(1)) == real, (
                f"{doc.name} diz {achado.group(1)} ferramentas MCP, o servidor "
                f"registra {real}. Ou corrija o numero, ou tire-o e aponte para "
                f"capabilities(), que pergunta ao servidor em vez de repetir."
            )


def test_handoff_declara_a_versao_corrente():
    """O cabecalho dizia core v0.9.0 com a arvore em v0.13.0."""
    import delegation_core

    cabecalho = HANDOFF.read_text(encoding="utf-8").split("\n## ")[0]
    versoes = re.findall(r"v(\d+\.\d+\.\d+)", cabecalho)
    assert versoes, "o cabecalho do HANDOFF nao declara mais nenhuma versao"
    assert delegation_core.__version__ in versoes, (
        f"HANDOFF.md abre declarando {versoes}, a arvore esta em "
        f"{delegation_core.__version__}. Esse cabecalho e a primeira coisa que "
        f"o proximo agente le."
    )


def test_conftest_existe_e_e_autouse():
    """A guarda de estado da suite nao pode sumir sem alguem notar.

    Ela foi escrita depois de um teste sobrescrever ~/.delegation_core/config.json
    e derrubar o daemon. Um conftest apagado devolve exatamente essa exposicao,
    em silencio.
    """
    conftest = (RAIZ / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert "autouse=True" in conftest
    assert "CONFIG_FILE" in conftest


# ── assinaturas que o AGENT_GUIDE promete ───────────────────────────────────

AGENT_GUIDE = RAIZ / "AGENT_GUIDE.md"


def _parametros_reais(nome_da_tool: str) -> set[str]:
    """Os parametros da funcao que o servidor realmente expoe, por AST.

    Por AST e nao por import: importar server.py levanta o FastMCP inteiro e
    puxa chromadb junto, o que este teste nao precisa e nao deve pagar.
    """
    arvore = ast.parse(SERVER.read_text(encoding="utf-8"))
    for no in ast.walk(arvore):
        if isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef)) and no.name == nome_da_tool:
            args = no.args
            return {a.arg for a in (args.posonlyargs + args.args + args.kwonlyargs)}
    pytest.fail(f"a ferramenta {nome_da_tool!r} sumiu de server.py")


def _parametros_ou_none(nome: str) -> set[str] | None:
    """Como _parametros_reais, mas devolve None em vez de falhar.

    Deliberadamente estreito. A primeira versao usava `except Exception:
    continue` em volta da busca, e como eu tinha esquecido o `import ast`, o
    NameError era engolido para TODA ferramenta: o teste varria a lista inteira,
    nao verificava nada, e passava verde sobre uma deriva que existia. O mesmo
    defeito que este arquivo existe para pegar, dentro do proprio arquivo.
    """
    arvore = ast.parse(SERVER.read_text(encoding="utf-8"))
    for no in ast.walk(arvore):
        if isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef)) and no.name == nome:
            args = no.args
            return {a.arg for a in (args.posonlyargs + args.args + args.kwonlyargs)}
    return None


def test_o_teste_de_assinatura_realmente_olha_alguma_coisa():
    """Guarda da guarda: pelo menos uma ferramenta do guia tem que ser achada.

    Sem isto, um erro que faca `_parametros_ou_none` devolver None sempre deixa
    o teste acima verde sem conferir nada.
    """
    assert _parametros_ou_none("relink_folder") is not None
    assert _parametros_ou_none("search_vault") is not None
    assert _parametros_ou_none("uma_ferramenta_que_nao_existe") is None


def test_o_guia_nao_promete_parametro_que_nao_existe():
    """Um agente que segue o AGENT_GUIDE tem que conseguir chamar a ferramenta.

    Medido em 03/09/2026: o guia documenta
    `relink_folder(folder, threshold=0.70)` e a assinatura real e
    `relink_folder(folder, days, min_similarity, max_links_per_note)`. Chamar
    como o guia manda devolve erro de validacao do pydantic, o que aconteceu
    de verdade nesta sessao. O parametro tem outro nome e ha dois que o guia
    nem menciona.

    capabilities() ja avisa que "prose has no guard against drifting from the
    code". Este teste e a guarda para as assinaturas que a prosa fixa.
    """
    texto = AGENT_GUIDE.read_text(encoding="utf-8")
    problemas = []

    # `### \`nome(param=valor, ...)\`` — as assinaturas que o guia declara.
    for achado in re.finditer(r"^### `(\w+)\(([^)]*)\)`", texto, re.MULTILINE):
        nome, assinatura = achado.group(1), achado.group(2)
        if not assinatura.strip():
            continue
        reais = _parametros_ou_none(nome)
        if reais is None:
            continue          # ferramenta que nao mora em server.py
        for parte in assinatura.split(","):
            param = parte.split("=")[0].strip()
            if param and param not in reais:
                problemas.append(f"{nome}({param}) — reais: {sorted(reais)}")

    assert not problemas, (
        "o AGENT_GUIDE documenta parametro que a ferramenta nao aceita:\n  "
        + "\n  ".join(problemas)
    )
