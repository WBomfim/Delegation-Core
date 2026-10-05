"""Nenhum ciclo de import entre os modulos do nucleo.

O grafo de codigo de 02/10/2026 mostrou dois: cli.py <-> doctor.py, e
ingest.py -> vault.py -> recuperacao.py -> ingest.py. Os dois eram imports
dentro de funcao, entao nada quebrava ao importar; mas cada ciclo amarra
modulos que deveriam poder mudar sozinhos, e um import movido para o topo
viraria erro de import parcial. Conta tambem import dentro de funcao, de
proposito. `graph/` e vendorizado e fica de fora.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACOTE = Path(__file__).resolve().parents[1] / "src" / "delegation_core"


def _modulos() -> dict[str, Path]:
    return {p.stem: p for p in PACOTE.glob("*.py") if p.stem not in ("__init__", "__main__")}


def _dependencias(caminho: Path, nomes: set[str]) -> set[str]:
    deps: set[str] = set()
    for no in ast.walk(ast.parse(caminho.read_text(encoding="utf-8"))):
        if isinstance(no, ast.ImportFrom) and no.level == 1:
            if no.module:
                raiz = no.module.split(".")[0]
                if raiz in nomes:
                    deps.add(raiz)
            else:
                deps.update(a.name for a in no.names if a.name in nomes)
    return deps


def _ciclos(grafo: dict[str, set[str]]) -> list[list[str]]:
    achados, estado, pilha = [], {}, []

    def visita(n: str) -> None:
        estado[n] = 1
        pilha.append(n)
        for m in sorted(grafo[n]):
            if estado.get(m) == 1:
                achados.append(pilha[pilha.index(m):] + [m])
            elif m not in estado:
                visita(m)
        pilha.pop()
        estado[n] = 2

    for n in sorted(grafo):
        if n not in estado:
            visita(n)
    return achados


def test_a_varredura_enxerga_imports_relativos():
    mods = _modulos()
    assert "vault" in _dependencias(mods["dashboard_api"], set(mods))


def test_a_deteccao_de_ciclo_funciona():
    assert _ciclos({"a": {"b"}, "b": {"a"}}) == [["a", "b", "a"]]


def test_nenhum_ciclo_de_import_no_nucleo():
    mods = _modulos()
    nomes = set(mods)
    grafo = {n: _dependencias(p, nomes) - {n} for n, p in mods.items()}
    ciclos = _ciclos(grafo)
    assert not ciclos, "ciclos de import: " + "; ".join(" -> ".join(c) for c in ciclos)
