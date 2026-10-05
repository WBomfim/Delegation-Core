"""Um tsconfig malformado nao pode apagar arquivos do grafo.

O comentario do proprio `extends` neste arquivo conta o desfecho de um campo com
tipo errado: "raised AttributeError ... which _safe_extract turned into a skip of
the whole file". A guarda foi posta no `extends` e nao nos campos vizinhos.

Medido, NOVE formas de tsconfig levantavam de `_read_tsconfig_aliases`, e cada
uma some com TODO arquivo cujo import passe por aquele tsconfig, nao so com o
alias. Uma decima nao levantava e era pior: `paths` com uma STRING no lugar da
lista fazia o laco percorrer os CARACTERES e produzir aliases de lixo, calado.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from delegation_core.graph.extractors.resolution import _read_tsconfig_aliases


def _le(tmp_path: Path, doc) -> dict:
    f = tmp_path / "tsconfig.json"
    f.write_text(json.dumps(doc), encoding="utf-8")
    achado = _read_tsconfig_aliases(f, tmp_path, seen=set())
    # Caminho absoluto do sistema: no Windows sai com "\\". As asserçoes
    # olham o sufixo, entao normaliza o separador.
    return {k: [str(v).replace("\\", "/") for v in vs] for k, vs in achado.items()}


MALFORMADOS = {
    "compilerOptions null": {"compilerOptions": None},
    "compilerOptions string": {"compilerOptions": "nada"},
    "compilerOptions lista": {"compilerOptions": []},
    "paths null": {"compilerOptions": {"paths": None}},
    "paths lista": {"compilerOptions": {"paths": ["@a/*"]}},
    "paths string": {"compilerOptions": {"paths": "@a/*"}},
    "raiz e lista": [1, 2, 3],
    "raiz e string": "abc",
    "raiz e numero": 7,
}


@pytest.mark.parametrize("nome", list(MALFORMADOS))
def test_malformado_devolve_vazio_em_vez_de_levantar(tmp_path: Path, nome):
    assert _le(tmp_path, MALFORMADOS[nome]) == {}


def test_alvo_string_nao_vira_lista_de_caracteres(tmp_path: Path):
    """O caso que NAO levantava e era o pior: sem a guarda, isto devolvia
    ['<base>', '/', '<base>/s', '/', '<base>/*']."""
    assert _le(tmp_path, {"compilerOptions": {"paths": {"@a/*": "./s/*"}}}) == {}


def test_um_alias_ruim_nao_derruba_os_bons(tmp_path: Path):
    """Degradar tem que ser por alias, e nao pelo arquivo inteiro."""
    achado = _le(tmp_path, {"compilerOptions": {"paths": {
        "@bom/*": ["./s/*"],
        "@ruim/*": "./x/*",
        "@outro/*": ["./t/*"],
    }}})
    assert set(achado) == {"@bom/*", "@outro/*"}


def test_baseUrl_invalido_cai_no_default_e_o_alias_sobrevive(tmp_path: Path):
    """Antes isto levantava TypeError em `base_dir / 5`. O alias nao tem culpa
    do baseUrl, entao ele resolve pelo default '.'."""
    achado = _le(tmp_path, {"compilerOptions": {"baseUrl": 5, "paths": {"@a/*": ["./s/*"]}}})
    assert list(achado) == ["@a/*"]
    assert achado["@a/*"][0].endswith("/s/*")


def test_o_caso_normal_nao_muda(tmp_path: Path):
    achado = _le(tmp_path, {"compilerOptions": {"paths": {"@a/*": ["./s/*"]}}})
    assert list(achado) == ["@a/*"]
    assert achado["@a/*"][0].endswith("/s/*")


def test_baseUrl_valido_continua_sendo_honrado(tmp_path: Path):
    """A regra que o comentario do codigo declara: paths sao relativos ao
    baseUrl, nao ao diretorio do tsconfig."""
    achado = _le(tmp_path, {"compilerOptions": {"baseUrl": "./src",
                                                "paths": {"@s/*": ["servicos/*"]}}})
    assert achado["@s/*"][0].endswith("/src/servicos/*")


def test_extends_com_lista_continua_funcionando(tmp_path: Path):
    """O campo que JA tinha guarda; preso para a correcao nao regredi-lo."""
    (tmp_path / "pai.json").write_text(json.dumps(
        {"compilerOptions": {"paths": {"@pai/*": ["./p/*"]}}}), encoding="utf-8")
    achado = _le(tmp_path, {"extends": ["./pai.json"],
                            "compilerOptions": {"paths": {"@filho/*": ["./f/*"]}}})
    assert set(achado) == {"@pai/*", "@filho/*"}


def test_filho_sobrescreve_o_pai(tmp_path: Path):
    """A regra do docstring: "Child config paths override parent"."""
    (tmp_path / "pai.json").write_text(json.dumps(
        {"compilerOptions": {"paths": {"@x/*": ["./do_pai/*"]}}}), encoding="utf-8")
    achado = _le(tmp_path, {"extends": "./pai.json",
                            "compilerOptions": {"paths": {"@x/*": ["./do_filho/*"]}}})
    assert achado["@x/*"][0].endswith("/do_filho/*")


def test_pai_malformado_nao_derruba_o_filho(tmp_path: Path):
    """A composicao dos dois: o pai quebrado degrada, e o filho segue valendo."""
    (tmp_path / "pai.json").write_text(json.dumps({"compilerOptions": None}), encoding="utf-8")
    achado = _le(tmp_path, {"extends": "./pai.json",
                            "compilerOptions": {"paths": {"@filho/*": ["./f/*"]}}})
    assert list(achado) == ["@filho/*"]
