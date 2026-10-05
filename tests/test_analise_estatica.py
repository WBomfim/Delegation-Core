"""Analise estatica do nucleo (hooks incluidos, em src/delegation_core/hooks): pyflakes (F) e erro de sintaxe (E9).

Existe por causa de `{_lang}`: uma variavel apagada em 03/09/2026 e esquecida
num f-string do `compress`. Nenhum teste chamava aquele caminho, a excecao
virava "Compression failed" em JSON, e o defeito ficou um mes em producao.
`ruff --select F821` o acusava na primeira linha.

`graph/` e vendorizado do Graphify e fica de fora. Sem ruff instalado o teste
e pulado, nao reprovado: `pip install -e .[dev]` o traz.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]


def _ruff() -> list[str] | None:
    try:
        import ruff  # noqa: F401
        return [sys.executable, "-m", "ruff"]
    except ImportError:
        caminho = shutil.which("ruff")
        return [caminho] if caminho else None


def test_nucleo_e_hooks_sem_achados_do_pyflakes():
    ruff = _ruff()
    if ruff is None:
        pytest.skip("ruff nao instalado (pip install -e .[dev])")
    r = subprocess.run(
        ruff + ["check", "--no-cache", "--select", "F,E9", "--output-format", "concise",
                "--exclude", "src/delegation_core/graph", "src/"],
        cwd=RAIZ, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
