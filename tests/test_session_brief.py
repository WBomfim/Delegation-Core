"""O brief de inicio de sessao atribuia a outras pessoas o trabalho da propria ferramenta.

O `SessionStart` hook e a primeira coisa que toda sessao do Claude Code le. Ele
lista as notas cujo mtime e maior que o do ultimo check, sob este cabecalho:

    New or updated notes (may be from Claude Desktop/Cowork or other sessions
    via export_session/write_note)

O criterio e `mtime > last_check`, e mais nada. Um passe de relink ou de
manutencao do proprio delegation-core reescreve o bloco `## Related` de centenas
de notas, e todas elas aparecem ali como se alguem as tivesse escrito noutra
sessao.

MEDIDO nesta maquina. O brief que abriu a sessao de 03/09 as 21:46 listou
`Fixes/2026-04-23-maxfiles-unused-windows-preprocessor.md` -- uma nota de ABRIL --
e fechou com "...and 248 more". Os carimbos eram todos de 15:03 a 15:28 do mesmo
dia, ou seja UM passe.

A distribuicao do vault inteiro nao deixa duvida sobre o que e o que::

        1 nota/min  :  34 minutos, somando     34 notas
      2-10 notas/min:  17 minutos, somando    100 notas
     11-20 notas/min:  10 minutos, somando    151 notas
     21-50 notas/min:   6 minutos, somando    193 notas
    51-200 notas/min:   4 minutos, somando    561 notas
      201+ notas/min:  15 minutos, somando  7.557 notas

Quinze minutos concentram 88% das 8.596 notas do vault. Ninguem escreve 690
notas em um minuto: aquilo e a ferramenta mexendo no proprio vault. O corte de
20 vem dai, e nao de gosto.

A correcao tem duas metades, e a primeira importa mais: o hook NAO TEM COMO
saber quem escreveu, entao ele para de afirmar que sabe. A segunda e colapsar o
lote numa linha, para o brief voltar a caber na cabeca de quem le.
"""
from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "src" / "delegation_core" / "hooks" / "session_start_brief.py"


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("session_start_brief", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _vault(tmp_path, quantidade: int, mesmo_minuto: bool):
    v = tmp_path / "vault"
    (v / "Reference").mkdir(parents=True)
    base = time.time() - 60
    for i in range(quantidade):
        f = v / "Reference" / f"nota-{i:04d}.md"
        f.write_text("x", encoding="utf-8")
        # 1s entre as notas do lote, e nao o MESMO instante: um passe real
        # escreve uma atras da outra, nao todas no mesmo microssegundo. A
        # primeira versao usava o mesmo mtime para todas e por isso
        # LOTE_INTERVALO_SEG=0 sobrevivia a mutacao -- o parametro escolhido por
        # medicao nao estava testado para o que ele faz.
        quando = (base + i) if mesmo_minuto else (base + i * 120)
        import os
        os.utime(f, (quando, quando))
    return v


def _rodar(hook, mod_vault, capsys, monkeypatch, tmp_path):
    estado = tmp_path / "brief.json"
    estado.write_text(json.dumps({"last_check": time.time() - 3600}), encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"vault_path": str(mod_vault),
                               "vault_folders": ["Reference"]}), encoding="utf-8")
    monkeypatch.setattr(hook, "STATE_PATH", estado)
    monkeypatch.setattr(hook, "CONFIG_PATH", cfg)
    monkeypatch.setattr(hook, "_trigger_reindex", lambda: False)
    monkeypatch.setattr(hook, "_trigger_maintenance", lambda: False)
    hook.main()
    return capsys.readouterr().out


# ── a afirmacao que o hook nao pode sustentar ───────────────────────────────


def test_o_cabecalho_nao_atribui_as_notas_a_outras_sessoes(hook, tmp_path, capsys, monkeypatch):
    v = _vault(tmp_path, 3, mesmo_minuto=False)
    saida = _rodar(hook, v, capsys, monkeypatch, tmp_path)

    assert "via export_session/write_note" not in saida, (
        "o hook so sabe que o mtime mudou; quem mudou ele nao sabe"
    )


def test_o_cabecalho_avisa_que_passes_da_propria_ferramenta_entram_na_conta(
        hook, tmp_path, capsys, monkeypatch):
    v = _vault(tmp_path, 3, mesmo_minuto=False)
    saida = _rodar(hook, v, capsys, monkeypatch, tmp_path)
    assert "relink" in saida.lower() or "maintenance" in saida.lower()


# ── o lote colapsado ────────────────────────────────────────────────────────


def test_um_lote_de_centenas_vira_uma_linha_e_nao_centenas(hook, tmp_path, capsys, monkeypatch):
    v = _vault(tmp_path, 300, mesmo_minuto=True)
    saida = _rodar(hook, v, capsys, monkeypatch, tmp_path)

    linhas_de_nota = [ln for ln in saida.splitlines() if ln.startswith("- `")]
    assert len(linhas_de_nota) <= 2, (
        f"300 notas do mesmo minuto viraram {len(linhas_de_nota)} linhas"
    )
    assert "300" in saida, "o numero tem que continuar aparecendo"


def test_o_lote_e_nomeado_como_lote(hook, tmp_path, capsys, monkeypatch):
    v = _vault(tmp_path, 300, mesmo_minuto=True)
    saida = _rodar(hook, v, capsys, monkeypatch, tmp_path)
    assert "lote" in saida.lower() or "batch" in saida.lower() or "bulk" in saida.lower()


def test_poucas_notas_espalhadas_continuam_listadas_uma_a_uma(hook, tmp_path, capsys, monkeypatch):
    """O caso que o brief existe para servir: tres notas escritas de verdade."""
    v = _vault(tmp_path, 3, mesmo_minuto=False)
    saida = _rodar(hook, v, capsys, monkeypatch, tmp_path)

    linhas_de_nota = [ln for ln in saida.splitlines() if ln.startswith("- `")]
    assert len(linhas_de_nota) == 3


def test_vinte_notas_na_mesma_corrida_nao_viram_lote(hook, tmp_path, capsys, monkeypatch):
    """O corte e 20 porque a medicao mostra que ate 20 e atividade plausivel de
    uma pessoa ou de um agente; acima disso, neste vault, e sempre passe da
    ferramenta.

    A asercao e sobre NAO colapsar, e nao sobre imprimir vinte linhas: MAX_NOTES
    ja e 8 e sempre foi, entao 20 notas soltas saem como 8 mais "and 12 more".
    A primeira versao deste teste afirmava 20 linhas e falhava contra um limite
    que nao tem nada a ver com o defeito.
    """
    v = _vault(tmp_path, 20, mesmo_minuto=True)
    saida = _rodar(hook, v, capsys, monkeypatch, tmp_path)

    assert "lote de" not in saida
    assert "and 12 more" in saida


def test_o_intervalo_separa_uma_corrida_da_nota_escrita_depois(hook, tmp_path, capsys, monkeypatch):
    """O que LOTE_INTERVALO_SEG realmente decide.

    30 notas seguidas de 1 em 1 segundo sao uma corrida. Uma nota escrita 45s
    depois do fim dela NAO e: 45 > 30, entao ela fica de fora do lote e aparece
    pelo nome, que e o ponto do brief.
    """
    import os
    v = tmp_path / "vault"
    (v / "Reference").mkdir(parents=True)
    base = time.time() - 600
    for i in range(30):
        f = v / "Reference" / f"lote-{i:03d}.md"
        f.write_text("x", encoding="utf-8")
        os.utime(f, (base + i, base + i))
    tarde = v / "Reference" / "escrita-45s-depois.md"
    tarde.write_text("y", encoding="utf-8")
    os.utime(tarde, (base + 29 + 45, base + 29 + 45))

    saida = _rodar(hook, v, capsys, monkeypatch, tmp_path)

    assert "escrita-45s-depois" in saida, "a nota separada por 45s foi engolida pelo lote"
    assert "lote de 30 notas" in saida


def test_um_lote_e_algumas_notas_soltas_aparecem_os_dois(hook, tmp_path, capsys, monkeypatch):
    """O caso real desta maquina: o passe de relink E as notas que uma sessao
    escreveu depois.

    Medido no vault do usuario com o corte de 30s: as 256 notas tocadas depois
    das 14:00 de 03/09 viraram 5 lotes cobrindo 254, e DUAS soltas -- que sao
    exatamente as duas notas escritas de verdade naquele dia.
    """
    import os

    v = tmp_path / "vault"
    (v / "Reference").mkdir(parents=True)
    base = time.time() - 3000
    for i in range(200):                      # o passe: uma nota por segundo
        f = v / "Reference" / f"do-lote-{i:03d}.md"
        f.write_text("x", encoding="utf-8")
        os.utime(f, (base + i, base + i))
    for j, atraso in enumerate((600, 1200)):  # escrita de verdade, bem depois
        f = v / "Reference" / f"escrita-de-verdade-{j}.md"
        f.write_text("y", encoding="utf-8")
        quando = base + 199 + atraso
        os.utime(f, (quando, quando))

    saida = _rodar(hook, v, capsys, monkeypatch, tmp_path)

    assert "escrita-de-verdade-0" in saida
    assert "escrita-de-verdade-1" in saida
    assert "lote de 200 notas" in saida
    assert "do-lote-" not in saida, "as notas do lote nao voltam a ser listadas"
