"""So o engine fala com o modelo local.

Todo pedido de geracao passa pelo DelegationEngine, que aplica orcamento,
fila, arbitro de GPU, idioma e deteccao de resposta vazia. Um modulo que
chamasse /v1/chat/completions por conta propria pularia tudo isso. A regra
estava escrita no docs/MAPA.md sem teste; agora tem.
"""

from __future__ import annotations

from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]


def _quem_chama_o_modelo() -> list[str]:
    achados = []
    for pasta in (RAIZ / "src" / "delegation_core", RAIZ / "hooks"):
        for arq in pasta.rglob("*.py"):
            rel = arq.relative_to(RAIZ).as_posix()
            if rel.startswith("src/delegation_core/graph/"):
                continue
            if "/v1/chat/completions" in arq.read_text(encoding="utf-8"):
                achados.append(rel)
    return sorted(achados)


def test_so_o_engine_chama_o_endpoint_do_modelo():
    assert _quem_chama_o_modelo() == ["src/delegation_core/engine.py"]
