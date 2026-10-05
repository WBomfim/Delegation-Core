"""`_merge_alias` inseria o alias cru numa estrutura YAML.

O alias vem do TITULO da nota. `compose_note` gera um alias de truncamento
quando `safe_filename` corta o nome em 50 caracteres, e o comentario que o
justifica diz por que ele existe: um link escrito com o titulo real precisa
resolver, e sem esse alias ele nao resolve.

Um titulo com virgula, dois-pontos ou `#` desfazia exatamente isso. MEDIDO em
03/09/2026, com `yaml.safe_load` lendo o resultado:

    inline  + virgula      ['Primeiro', 'Reuniao ... item 2', 'tetos de bolsao']
                           o alias virou DOIS
    inline  + `]`          YAML INVALIDO, o bloco inteiro deixa de ser lido
    inline  + `: `         ['Primeiro', {'Decisao': 'migrar para OAuth2'}]
                           o alias virou um DICIONARIO dentro da lista
    bloco   + `: `         mesmo dicionario
    bloco   + `#`          ['Primeiro', 'Sprint'] -- truncado no hash, que
                           abre comentario em YAML

`yaml_quote_scalar` existe neste mesmo modulo, quinze linhas acima, e o
docstring dela diz o que ela e para: "An unquoted scalar containing ': ' is
ambiguous/invalid YAML (Obsidian and any strict frontmatter parser will choke
on it) - quote unconditionally so titles are safe regardless of content."
`_merge_alias` nao a chamava em nenhum dos dois ramos.

POR QUE NINGUEM VIU: `linker.frontmatter_aliases` nao usa YAML, e um regex
proprio, e devolve a linha inteira como string. O leitor do projeto ACERTA.
Quem erra e o YAML, ou seja, o Obsidian e qualquer consumidor externo. O vault
ficava legivel so pelos parsers tolerantes do proprio projeto.

EXPOSICAO REAL neste vault, contada antes de mexer: 8.596 notas com
frontmatter, ZERO com `aliases:` em lista inline. Latente, nao ativo. Corrigido
mesmo assim pelo criterio ja usado no downloader em 03/09: quando o risco vem
de dado FORA do meu alcance -- aqui o titulo, e a forma `aliases: [A, B]` que
qualquer editor escreve -- o zero de hoje nao e uma garantia sobre amanha.
"""
from __future__ import annotations

import pytest
import yaml

from delegation_core.notes import _merge_alias


def _aliases(frontmatter: str):
    return yaml.safe_load(frontmatter)["aliases"]


# ── lista inline ────────────────────────────────────────────────────────────


def test_virgula_no_titulo_nao_parte_o_alias_em_dois():
    alias = "Reuniao com o cliente - regua do item 2, tetos de bolsao"
    saida = _merge_alias("aliases: [Primeiro]", alias)
    assert _aliases(saida) == ["Primeiro", alias]


def test_dois_pontos_no_titulo_nao_vira_dicionario():
    alias = "Decisao: migrar para OAuth2"
    saida = _merge_alias("aliases: [Primeiro]", alias)
    assert _aliases(saida) == ["Primeiro", alias]


def test_colchete_no_titulo_nao_quebra_o_bloco():
    alias = "Fecha colchete] no titulo"
    saida = _merge_alias("aliases: [Primeiro]", alias)
    assert _aliases(saida) == ["Primeiro", alias]


def test_aspas_no_titulo_sobrevivem():
    alias = 'Nota com "aspas" no titulo'
    saida = _merge_alias("aliases: [Primeiro]", alias)
    assert _aliases(saida) == ["Primeiro", alias]


def test_lista_inline_vazia_recebe_o_primeiro():
    saida = _merge_alias("aliases: []", "Um, com virgula")
    assert _aliases(saida) == ["Um, com virgula"]


# ── lista em bloco ──────────────────────────────────────────────────────────


def test_dois_pontos_em_lista_de_bloco_nao_vira_dicionario():
    alias = "Decisao: migrar para OAuth2"
    saida = _merge_alias('aliases:\n  - "Primeiro"', alias)
    assert _aliases(saida) == ["Primeiro", alias]


def test_hash_em_lista_de_bloco_nao_e_truncado():
    """`#` abre comentario em YAML: 'Sprint #3 do time' virava 'Sprint'."""
    alias = "Sprint #3 do time"
    saida = _merge_alias('aliases:\n  - "Primeiro"', alias)
    assert _aliases(saida) == ["Primeiro", alias]


def test_virgula_em_lista_de_bloco_continua_inteira():
    """Este ramo ja estava certo para virgula. Pinado para nao regredir."""
    alias = "Reuniao com o cliente - item 2, tetos"
    saida = _merge_alias('aliases:\n  - "Primeiro"', alias)
    assert _aliases(saida) == ["Primeiro", alias]


# ── o que nao pode mudar ────────────────────────────────────────────────────


def test_alias_repetido_nao_e_inserido_de_novo():
    fm = 'aliases:\n  - "Primeiro"'
    assert _merge_alias(fm, "primeiro") == fm


def test_alias_repetido_em_lista_inline_tambem_nao():
    fm = "aliases: [Primeiro]"
    assert _merge_alias(fm, "PRIMEIRO") == fm


def test_frontmatter_sem_aliases_volta_intacto():
    fm = "title: x\ndate: 2026-09-04"
    assert _merge_alias(fm, "Novo") == fm


def test_o_leitor_do_projeto_continua_lendo_o_alias_inteiro():
    """`frontmatter_aliases` ja acertava; as aspas nao podem estragar isso."""
    from delegation_core.linker import frontmatter_aliases

    alias = "Decisao: migrar para OAuth2"
    corpo = _merge_alias("aliases: [Primeiro]", alias)
    nota = f"---\n{corpo}\n---\n\ncorpo\n"

    assert alias in frontmatter_aliases(nota)
