"""`yaml_quote_scalar` promete seguranca "regardless of content" e nao entrega.

Achado por TESTE DE PROPRIEDADE, um metodo que ainda nao tinha sido usado nesta
suite. A propriedade que interessa nao e "escapa aspas": e "o que sai daqui,
colado num frontmatter, e lido de volta pelo YAML como o mesmo valor".

    doc = "titulo: " + yaml_quote_scalar(s)
    yaml.safe_load(doc)["titulo"] == s      para todo s

Hypothesis derrubou isso em poucas dezenas de exemplos, com `\\x1f`. Varrendo
os 256 primeiros codepoints um a um: SESSENTA E QUATRO quebram ou deformam o
bloco, entre eles `\\n` e `\\r`.

A funcao escapa `\\` e `"` e mais nada. Um caractere de controle dentro de um
escalar entre aspas duplas precisa sair como `\\xNN`, senao o leitor de YAML
recusa o documento INTEIRO -- que e o mesmo desfecho das duas notas deste vault
com frontmatter ilegivel, e essa funcao existe justamente para impedi-lo. O
docstring dela diz: "quote unconditionally so titles are safe regardless of
content".

## EXPOSICAO REAL HOJE: ZERO

Medido no vault: nenhuma das 8.596 notas tem caractere de controle no
frontmatter. O hook de transcricao, que e a fonte de titulo mais exposta a texto
cru, faz `.split("\\n")[0]` e fecha o caminho da quebra de linha.

Corrigido assim mesmo, pelo criterio ja usado no downloader e no `_merge_alias`:
`create_note(title=...)` aceita qualquer string de qualquer chamador MCP, e
`synthesize`/`classify` derivam titulo do CONTEUDO de documentos ingeridos. O
zero de hoje e sobre o que ja entrou, nao sobre o que vai entrar.
"""
from __future__ import annotations

import pytest
import yaml

from delegation_core.notes import yaml_quote_scalar, yaml_unquote_scalar

#: Amostra fixa que cobre as familias que a propriedade exercita: vazio, ascii,
#: acento, travessao, aspas, barra, controle, e os pares de escape do YAML.
_AMOSTRA = [
    "", " ", "titulo comum", "Validação — Sotéria", 'com "aspas"',
    "com \\ barra", "com : dois pontos", "com # hash", "[colchete]",
    "a\nb", "a\rb", "a\tb", "a\x00b", "a\x1fb", "a\x7fb", "a\x85b",
    "\\", '"', '\\"', "emoji 🌙", "  espaco nas pontas  ",
]


@pytest.mark.parametrize("s", _AMOSTRA)
def test_o_citado_e_lido_de_volta_como_o_mesmo_valor(s):
    """A propriedade que importa, e a que estava quebrada."""
    doc = "titulo: " + yaml_quote_scalar(s)
    assert yaml.safe_load(doc)["titulo"] == s


@pytest.mark.parametrize("s", _AMOSTRA)
def test_o_reversor_do_projeto_tambem_devolve_o_mesmo_valor(s):
    """`yaml_unquote_scalar` existe para o parser ingenuo do proprio projeto ler
    de volta. Se a citacao ganha escape novo, ele tem que decodificar."""
    assert yaml_unquote_scalar(yaml_quote_scalar(s)) == s


@pytest.mark.parametrize("cp", [0x00, 0x07, 0x0a, 0x0d, 0x1b, 0x1f, 0x7f])
def test_cada_caractere_de_controle_que_quebrava(cp):
    valor = f"antes{chr(cp)}depois"
    doc = "titulo: " + yaml_quote_scalar(valor)

    assert yaml.safe_load(doc)["titulo"] == valor


def test_nenhum_dos_256_primeiros_codepoints_quebra():
    """A varredura que mediu o defeito, virada teste."""
    ruins = []
    for cp in range(0x100):
        valor = f"a{chr(cp)}b"
        try:
            if yaml.safe_load("titulo: " + yaml_quote_scalar(valor))["titulo"] != valor:
                ruins.append(hex(cp))
        except Exception:
            ruins.append(hex(cp))
    assert not ruins, f"{len(ruins)} codepoints quebram o frontmatter: {ruins[:12]}"


def test_um_titulo_comum_nao_ganha_escape_nenhum():
    """Escapar nao pode deformar o caso de todo mundo."""
    assert yaml_quote_scalar("Reuniao com o cliente 03-09") == '"Reuniao com o cliente 03-09"'


def test_acento_e_travessao_continuam_literais():
    """Escape de controle nao pode virar escape de tudo: o vault e em portugues."""
    citado = yaml_quote_scalar("Validação de Agentes — Sotéria")
    assert "ã" in citado and "é" in citado
    assert yaml.safe_load("t: " + citado)["t"] == "Validação de Agentes — Sotéria"


def test_aspas_e_barra_continuam_escapadas():
    """A metade que ja funcionava, pinada."""
    valor = 'com "aspas" e \\ barra'
    assert yaml.safe_load("t: " + yaml_quote_scalar(valor))["t"] == valor
