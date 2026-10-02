"""A suite nao escreve no estado real da maquina, com qualquer nome de caminho.

Existe por causa de 29/09/2026: `ingest._REGISTRY_FILE` escapava do conftest,
um teste novo gravou por cima do `ingested_sources.json` real, e em duas
maquinas o registro de ingestao virou uma fonte so do pytest. Num Mac isso fez a
reconstrucao do indice pular todas as fontes externas.
"""

import sys
from pathlib import Path

#: Calculado no import do teste, antes de qualquer fixture rodar.
_ESTADO_REAL = (Path.home() / ".delegation_core").absolute()


def test_nenhum_caminho_de_modulo_aponta_para_o_estado_real():
    escapados = []
    for nome, mod in list(sys.modules.items()):
        if not nome.startswith("delegation_core") or mod is None:
            continue
        for atributo, valor in vars(mod).items():
            if isinstance(valor, Path) and valor.absolute().is_relative_to(_ESTADO_REAL):
                escapados.append(f"{nome}.{atributo} = {valor}")
    assert not escapados, "caminhos que a suite ainda alcanca de verdade: " + "; ".join(escapados)


def test_o_registro_de_ingestao_grava_no_temporario():
    from delegation_core import ingest

    real = _ESTADO_REAL / "ingested_sources.json"
    antes = real.stat().st_mtime_ns if real.exists() else None

    ingest._save_registry({"/fonte/do/teste": {"files": {}}})

    assert not ingest._REGISTRY_FILE.absolute().is_relative_to(_ESTADO_REAL)
    assert ingest._load_registry() == {"/fonte/do/teste": {"files": {}}}
    depois = real.stat().st_mtime_ns if real.exists() else None
    assert antes == depois, "o teste escreveu no ingested_sources.json real"
