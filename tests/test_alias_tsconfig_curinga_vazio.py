"""Um caminho resolvido nunca pode conter um asterisco literal.

`_resolve_tsconfig_alias` condicionava a substituicao do curinga a `captured`
ser verdadeiro. Um casamento vazio e falsy, entao o alvo voltava intacto, com o
`*` dentro, e um caminho com asterisco nao resolve para nada nem pode: a aresta
nascia apontando para um arquivo impossivel e o asterisco entrava no id do no.

    padrao "@lib/*.css"  import "@lib/.css"  ->  "estilos/*.css"
    padrao "*.js"        import ".js"        ->  "js/*.js"

Os dois casos sao plausiveis: importar um arquivo cujo nome comeca com ponto.
"""
from __future__ import annotations

import pytest

from delegation_core.graph.extractors.resolution import (_match_tsconfig_alias,
                                                         _resolve_tsconfig_alias)


ALIASES = {
    "@app/*": ["./src/*"],
    "@lib/*.css": ["./estilos/*.css"],
    "*.js": ["./js/*.js"],
    "*": ["./raiz/*"],
    "@exato": ["./exato.ts"],
    "@dir/": ["./dir"],
}


@pytest.mark.parametrize("bruto", [
    "@app/x", "@app/", "@lib/.css", "@lib/a.css", "", ".js", "x.js",
    "@exato", "@dir/sub", "@app/a/b/c",
])
def test_nenhuma_resolucao_carrega_asterisco_literal(bruto):
    """A invariante, sobre todo import que casa algum padrao."""
    res = _resolve_tsconfig_alias(bruto, ALIASES)
    if res is None:
        return
    assert "*" not in str(res), f"{bruto!r} resolveu para {res!r}"


def test_curinga_vazio_da_o_caminho_sem_o_curinga():
    """O caso concreto: o ponto do arquivo e o proprio inicio do nome."""
    assert str(_resolve_tsconfig_alias("@lib/.css", ALIASES)).replace("\\", "/") == "estilos/.css"
    assert str(_resolve_tsconfig_alias(".js", ALIASES)).replace("\\", "/") == "js/.js"


def test_curinga_normal_continua_substituindo():
    assert str(_resolve_tsconfig_alias("@app/x", ALIASES)).replace("\\", "/") == "src/x"
    assert str(_resolve_tsconfig_alias("@lib/tema.css", ALIASES)).replace("\\", "/") == "estilos/tema.css"


def test_alias_exato_nao_e_afetado():
    """O ramo sem curinga nao passa pela substituicao e nao pode mudar."""
    assert str(_resolve_tsconfig_alias("@exato", ALIASES)).replace("\\", "/") == "exato.ts"


def test_prefixo_de_diretorio_nao_e_afetado():
    """Sem o curinga pega-tudo no conjunto, porque ele venceria: a regra que o
    docstring de _match_tsconfig_alias declara e que o prefixo de diretorio so
    entra DEPOIS de todo curinga real."""
    sem_pega_tudo = {k: v for k, v in ALIASES.items() if k not in ("*", "*.js")}
    assert str(_resolve_tsconfig_alias("@dir/sub", sem_pega_tudo)).replace("\\", "/") == "dir/sub"


def test_curinga_real_vence_prefixo_de_diretorio():
    """A regra do docstring, prendida como comportamento: com o pega-tudo no
    conjunto, e ele que ganha, e nao o prefixo de diretorio mais especifico.
    Foi este teste que me pegou escrevendo a expectativa errada."""
    assert str(_resolve_tsconfig_alias("@dir/sub", ALIASES)).replace("\\", "/") == "raiz/@dir/sub"


def test_a_ordem_de_especificidade_continua_valendo():
    """Exato ganha de curinga, e curinga de prefixo mais longo ganha do curto.
    Preso aqui porque a correcao mexe no mesmo laco que escolhe o vencedor."""
    aliases = {"*": ["./curto/*"], "@a/*": ["./longo/*"], "@a/exato": ["./exato.ts"]}
    assert str(_resolve_tsconfig_alias("@a/exato", aliases)).replace("\\", "/") == "exato.ts"
    assert str(_resolve_tsconfig_alias("@a/outro", aliases)).replace("\\", "/") == "longo/outro"
    assert str(_resolve_tsconfig_alias("zzz", aliases)).replace("\\", "/") == "curto/zzz"


def test_o_casamento_vazio_realmente_acontece():
    """Prende a premissa do resto do arquivo: sem isso os testes acima passariam
    por nunca chegarem no ramo que interessa."""
    resultado = _match_tsconfig_alias("@lib/.css", "@lib/*.css")
    assert resultado is not None
    _, capturado, curinga = resultado
    assert curinga is True
    assert capturado == "", "o trecho casado tem que ser vazio, que e a condicao falsy"
