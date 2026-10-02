"""A trava que impede o update perdido no registro de ingestao.

Estes testes existem por um caso real: em 31/08/2026 duas ingestoes correram
juntas e as entradas de Soteria e Pessoal sumiram do `ingested_sources.json`.
O arquivo continuou valido, ninguem viu na hora, e a busca apenas parou de
achar as duas pastas.

O teste central e `test_duas_escritas_concorrentes_nao_perdem_entrada`, que
reproduz a corrida com processos de verdade e falharia contra o codigo de
antes deste modulo.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from delegation_core.locking import (
    TravaIndisponivel,
    arquivo_travado,
    ler_modificar_escrever,
)


def test_a_trava_e_exclusiva_entre_threads(tmp_path):
    """Duas threads nao entram no bloco ao mesmo tempo."""
    alvo = tmp_path / "registro.json"
    dentro, maximo = 0, 0
    trava_py = threading.Lock()

    def trabalha():
        nonlocal dentro, maximo
        with arquivo_travado(alvo):
            with trava_py:
                dentro += 1
                maximo = max(maximo, dentro)
            time.sleep(0.05)
            with trava_py:
                dentro -= 1

    ts = [threading.Thread(target=trabalha) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert maximo == 1, f"{maximo} threads estiveram dentro do bloco juntas"


def test_duas_escritas_concorrentes_nao_perdem_entrada(tmp_path):
    """O caso de 31/08, reproduzido com processos de verdade.

    Dois processos leem o mesmo registro, cada um acrescenta a propria chave, e
    os dois gravam. Sem trava, o segundo a gravar apaga a entrada do primeiro,
    que foi exatamente o que aconteceu com Soteria e Pessoal.

    Processos e nao threads de proposito: a corrida que importa e entre
    processos, e uma trava que so funcionasse dentro de um processo passaria
    num teste de threads e falharia em producao.
    """
    alvo = tmp_path / "registro.json"
    alvo.write_text("{}", encoding="utf-8")

    programa = textwrap.dedent("""
        import json, sys, time
        from pathlib import Path
        sys.path.insert(0, %r)
        from delegation_core.locking import ler_modificar_escrever

        alvo = Path(sys.argv[1]); chave = sys.argv[2]

        def ler():
            return json.loads(alvo.read_text(encoding="utf-8"))

        def modificar(d):
            time.sleep(0.2)          # alarga a janela da corrida
            d[chave] = {"indexado": 1}
            return d

        def escrever(d):
            alvo.write_text(json.dumps(d), encoding="utf-8")

        ler_modificar_escrever(alvo, ler, escrever, modificar)
    """) % str(Path(__file__).resolve().parents[1] / "src")

    script = tmp_path / "escritor.py"
    script.write_text(programa, encoding="utf-8")

    procs = [subprocess.Popen([sys.executable, str(script), str(alvo), nome])
             for nome in ("soteria", "pessoal")]
    for p in procs:
        assert p.wait(timeout=60) == 0

    final = json.loads(alvo.read_text(encoding="utf-8"))
    assert set(final) == {"soteria", "pessoal"}, (
        f"uma entrada foi perdida: sobrou {sorted(final)}")


def test_a_trava_nao_e_pega_no_proprio_arquivo(tmp_path):
    """O `.lock` fica ao lado, e o alvo pode ser substituido por os.replace.

    Travar o proprio alvo soltaria a trava em silencio na hora da escrita
    atomica, porque `os.replace` troca o inode debaixo dela.
    """
    alvo = tmp_path / "registro.json"
    with arquivo_travado(alvo):
        assert (tmp_path / "registro.json.lock").exists()
        assert not alvo.exists(), "o alvo nao precisa existir para ser travado"


def test_espera_esgotada_levanta_tipo_proprio(tmp_path):
    """Nao conseguir a trava e diferente de o arquivo nao abrir."""
    alvo = tmp_path / "registro.json"
    with arquivo_travado(alvo):
        with pytest.raises(TravaIndisponivel):
            with arquivo_travado(alvo, espera=0.2):
                pass


def test_ler_modificar_escrever_devolve_o_que_gravou(tmp_path):
    alvo = tmp_path / "registro.json"
    alvo.write_text('{"a": 1}', encoding="utf-8")

    novo = ler_modificar_escrever(
        alvo,
        ler=lambda: json.loads(alvo.read_text(encoding="utf-8")),
        escrever=lambda d: alvo.write_text(json.dumps(d), encoding="utf-8"),
        modificar=lambda d: {**d, "b": 2},
    )
    assert novo == {"a": 1, "b": 2}
    assert json.loads(alvo.read_text(encoding="utf-8")) == {"a": 1, "b": 2}
