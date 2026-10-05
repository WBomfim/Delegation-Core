"""Um arquivo que a manutencao nunca consegue tirar do inbox disparava-a para sempre.

MEDIDO rodando o pipeline de manutencao DUAS vezes seguidas sobre o mesmo inbox:

    passada 1  skipped=['foto.png', 'LICENSE']
               junk=['README.md: skipped - matches boilerplate filename pattern']
               ficam no _inbox: ['LICENSE', 'foto.png']
    passada 2  skipped=['foto.png', 'LICENSE']
               junk=[]
               ficam no _inbox: ['LICENSE', 'foto.png']

Sao dois filtros diferentes com destinos diferentes, e so um deles esvazia o
inbox:

    junk      arquivo SUPORTADO cujo nome ou conteudo e boilerplate ->
              movido para _processed/
    skipped   arquivo cuja EXTENSAO nao e suportada -> fica onde esta

Deixar o arquivo nao suportado no lugar e o desenho, e esta escrito no
AGENT_GUIDE: "tell the user which files cannot be processed and what format to
convert them to". O problema e a combinacao com o gatilho deste hook, que
disparava manutencao sempre que o inbox tivesse QUALQUER arquivo:

    inbox_count = sum(1 for f in inbox.iterdir() if f.is_file())
    if inbox_count and (now - last_maintenance) > MAINTENANCE_COOLDOWN:

Um `.png` esquecido no inbox faz `delegation-core maintain` rodar a cada 30
minutos, para sempre, sem nunca poder mudar nada.

## A CORRECAO NAO DUPLICA A LISTA DE FORMATOS

O hook e stdlib-only por desenho e nao pode importar `SUPPORTED`. Copiar a lista
para ca seria criar a copia que deriva, que e o defeito que este job passou a
noite corrigindo.

Em vez disso o gatilho passa a perguntar outra coisa: o inbox MUDOU desde a
ultima vez? Um conjunto de arquivos que a manutencao ja viu e nao conseguiu
processar nao merece uma segunda tentativa identica. Nao precisa saber quais
formatos existem para saber que nada mudou.
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


@pytest.fixture
def cenario(hook, tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "Reference").mkdir(parents=True)
    (vault / "_inbox").mkdir()
    estado = tmp_path / "brief.json"
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"vault_path": str(vault),
                               "vault_folders": ["Reference"]}), encoding="utf-8")
    monkeypatch.setattr(hook, "STATE_PATH", estado)
    monkeypatch.setattr(hook, "CONFIG_PATH", cfg)
    monkeypatch.setattr(hook, "_trigger_reindex", lambda: False)

    disparos = []
    monkeypatch.setattr(hook, "_trigger_maintenance",
                        lambda: (disparos.append(1), True)[1])

    def rodar(ultimo_check_ha=3600):
        """Uma sessao. PRESERVA o estado que a anterior gravou, so recuando o
        relogio: a primeira versao reescrevia o arquivo inteiro e apagava a
        marca do inbox, entao o teste media a fixture e nao o codigo."""
        atual = json.loads(estado.read_text(encoding="utf-8")) if estado.exists() else {}
        atual["last_check"] = time.time() - ultimo_check_ha
        atual["last_maintenance_trigger"] = 0
        estado.write_text(json.dumps(atual), encoding="utf-8")
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            hook.main()
        return len(disparos)

    return vault, rodar, disparos


def test_o_mesmo_inbox_intocado_nao_dispara_duas_vezes(cenario):
    """O laco: um .png esquecido rodava manutencao a cada 30 minutos, para sempre."""
    vault, rodar, disparos = cenario
    (vault / "_inbox" / "foto.png").write_bytes(b"\x89PNG")

    rodar()
    primeiro = len(disparos)
    rodar()

    assert primeiro == 1, "a primeira vez tem que disparar"
    assert len(disparos) == 1, "o mesmo inbox, intocado, nao merece segunda tentativa"


def test_um_arquivo_novo_no_inbox_dispara_de_novo(cenario):
    """A correcao nao pode desligar o gatilho: o proposito dele e reagir a
    arquivo novo."""
    vault, rodar, disparos = cenario
    (vault / "_inbox" / "foto.png").write_bytes(b"\x89PNG")
    rodar()

    (vault / "_inbox" / "documento.md").write_text("# novo\n", encoding="utf-8")
    rodar()

    assert len(disparos) == 2


def test_um_arquivo_removido_do_inbox_tambem_conta_como_mudanca(cenario):
    vault, rodar, disparos = cenario
    (vault / "_inbox" / "a.md").write_text("a", encoding="utf-8")
    (vault / "_inbox" / "b.md").write_text("b", encoding="utf-8")
    rodar()

    (vault / "_inbox" / "b.md").unlink()
    rodar()

    assert len(disparos) == 2


def test_inbox_vazio_nunca_dispara(cenario):
    vault, rodar, disparos = cenario
    rodar()
    assert disparos == []


def test_o_conteudo_mudando_no_mesmo_nome_conta(cenario):
    """Alguem substitui o arquivo pelo mesmo nome: e material novo."""
    import os
    vault, rodar, disparos = cenario
    alvo = vault / "_inbox" / "doc.md"
    alvo.write_text("primeira versao", encoding="utf-8")
    rodar()

    alvo.write_text("segunda versao, bem diferente", encoding="utf-8")
    os.utime(alvo, (time.time() + 10, time.time() + 10))
    rodar()

    assert len(disparos) == 2
