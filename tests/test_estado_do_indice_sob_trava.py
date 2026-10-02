"""Dois escritores do `.chroma_index.json` nao podem apagar o carimbo um do outro.

## O defeito, medido em 26/09/2026

O arquivo de estado do indice diz quais notas ja estao embutidas e com que
mtime, e e o que o reindex incremental consulta para pular o que nao mudou.
Tres caminhos o escrevem, todos por ler-modificar-escrever: `stamp_indexed`,
`delete_notes` e o fim do `reindex_vault`.

A escrita era atomica e **nao** era travada, e as duas coisas tem nomes
parecidos e resolvem problemas diferentes: atomica garante que ninguem le o
arquivo pela metade, trava garante que quem leu antes de voce ainda esteja no
que voce grava. O proprio docstring do metodo de escrita falava da primeira e
nada cuidava da segunda.

A concorrencia nao e hipotetica, e vem dos hooks de sessao:

- o hook de fim de sessao dispara `delegation-core reindex` destacado
- o hook de inicio dispara `delegation-core maintain` destacado
- `jobs.submit` abre uma thread nova **sem checar se ja existe um job igual
  rodando**, entao dois pedidos viram dois escritores

Duas sessoes fechando juntas, ou uma fechando enquanto outra abre, bastam.

Consequencias medidas, as duas:

1. **Carimbo perdido.** O ultimo a gravar escreve por cima, e as notas
   carimbadas pelo outro voltam a parecer intocadas: o proximo reindex
   reembute nota que ja estava correta.
2. **Estado ilegivel.** O temporario tinha nome FIXO, `.json.tmp`. Dois
   escritores abriam o mesmo arquivo, um truncava o do outro no meio do dump, e
   o `os.replace` instalava o resultado. `carregar_estado` le arquivo quebrado
   como "nada esta carimbado", que custa reembutir o vault inteiro, cerca de 11
   minutos e 8.594 notas nesta maquina.

E a terceira vez que esta familia aparece no projeto: `jobs.py` em 03/09 e o
registro de ingestao em 31/08, que foi o defeito que fez o `locking.py` existir.
A correcao aqui e aplicar o modulo que ja estava escrito.

## Por que a trava indisponivel NAO derruba a operacao

Um carimbo perdido custa reembutir uma nota. Derrubar a escrita da nota, ou o
fim de uma sessao, por nao conseguir a trava de um carimbo seria trocar um
custo de desempenho por perda de trabalho. O ciclo avisa e segue.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from delegation_core import notes
from delegation_core.config import Config


@pytest.fixture
def estado(tmp_path):
    return notes.caminho_do_estado(tmp_path)


#: Janela entre ler e gravar. Generosa de proposito: a versao sem trava tem que
#: perder a entrada TODA vez, senao o teste de controle abaixo vira moeda.
JANELA = 0.25


def _duas_threads(escrever_um):
    a = threading.Thread(target=escrever_um, args=("Reference/a.md",))
    b = threading.Thread(target=escrever_um, args=("Reference/b.md",))
    a.start(); b.start(); a.join(timeout=15); b.join(timeout=15)


def test_carimbo_de_um_nao_apaga_o_do_outro(estado):
    """A LIGACAO com o defeito real, reproduzida com duas threads.

    Cada uma le, espera a janela que existia, e grava a propria entrada. Com a
    trava os dois ciclos serializam e as duas entradas sobrevivem.

    A primeira versao deste teste usava um `Barrier` para forcar os dois a lerem
    antes de qualquer gravar, e travou: a trava e exatamente o que impede isso,
    entao o barrier nunca podia ser satisfeito. O deadlock era a prova de que a
    trava funciona, e um teste ruim.

    Conferido por mutacao: e o `test_o_teste_reconheceria_a_corrida` logo abaixo
    que faz o papel da mutacao aqui, rodando o MESMO cenario sem trava e
    exigindo que uma entrada se perca.
    """
    def escreve(chave):
        def modificar(atual):
            time.sleep(JANELA)
            return {**atual, chave: 1.0}
        notes.atualizar_estado(estado, modificar)

    _duas_threads(escreve)

    final = json.loads(estado.read_text(encoding="utf-8"))
    assert set(final) == {"Reference/a.md", "Reference/b.md"}, (
        f"um dos dois carimbos foi perdido: {sorted(final)}")


def test_o_teste_reconheceria_a_corrida(estado):
    """O controle, que e o que impede o teste acima de passar por vacuidade.

    Mesmo cenario, mesma janela, ler-modificar-escrever SEM trava: e o codigo
    que estava em producao ate 26/09/2026. Se isto passar a nao perder entrada,
    a janela encolheu e o teste acima deixou de provar o que diz provar.
    """
    def escreve_sem_trava(chave):
        atual = notes.carregar_estado(estado)
        time.sleep(JANELA)
        notes.gravar_estado(estado, {**atual, chave: 1.0})

    _duas_threads(escreve_sem_trava)

    final = json.loads(estado.read_text(encoding="utf-8"))
    assert len(final) == 1, (
        "sem trava as duas entradas sobreviveram, entao a janela nao esta "
        f"sendo exercitada e o teste irmao nao prova nada: {sorted(final)}")


def test_o_temporario_nao_e_compartilhado_entre_escritores(estado, monkeypatch):
    """O segundo defeito: nome de temporario fixo.

    Sob trava isso nao morde, mas o temporario e a ultima defesa de quem grave
    por um caminho que a trava nao cubra, e um nome unico custa uma linha.
    """
    vistos = []
    real_replace = notes.os.replace

    def espiando(origem, destino):
        vistos.append(str(origem))
        return real_replace(origem, destino)

    monkeypatch.setattr(notes.os, "replace", espiando)
    notes.gravar_estado(estado, {"a": 1.0})

    assert vistos, "nada foi gravado; o teste nao prova nada"
    assert not vistos[0].endswith(".json.tmp"), (
        f"o temporario voltou ao nome fixo compartilhado: {vistos[0]}")
    assert str(threading.get_ident()) in vistos[0], (
        "o temporario nao carrega a thread, entao duas threads o compartilham")


def test_estado_ilegivel_responde_vazio_e_avisa(estado, caplog):
    """Arquivo quebrado le como "nada carimbado", em voz alta.

    Responder {} e correto, porque reembutir e lento e nao errado. Ser
    silencioso nao e: o sintoma e um reindex "incremental" que roda por minutos
    sem nada dizendo por que.
    """
    estado.write_text("{isto nao e json", encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert notes.carregar_estado(estado) == {}
    assert any("ilegivel" in r.message or "ilegivel" in str(r.getMessage())
               for r in caplog.records), "leu arquivo quebrado em silencio"


def test_trava_ocupada_nao_derruba_o_carimbo(estado, monkeypatch, caplog):
    """Preferir carimbo perdido a trabalho perdido, e dizer qual dos dois foi.

    Conferido por mutacao: deixar a `TravaIndisponivel` subir derruba este
    teste, e no sistema real derrubaria a escrita da nota por causa do carimbo.
    """
    from delegation_core import locking

    def sempre_ocupada(*a, **k):
        raise locking.TravaIndisponivel("ocupada no teste")

    monkeypatch.setattr(locking, "ler_modificar_escrever", sempre_ocupada)
    with caplog.at_level("WARNING"):
        r = notes.atualizar_estado(estado, lambda atual: {**atual, "x": 1.0})

    assert r == {"x": 1.0}, "o carimbo tinha que acontecer mesmo sem trava"
    assert json.loads(estado.read_text(encoding="utf-8")) == {"x": 1.0}
    assert any("sem trava" in r.getMessage() for r in caplog.records), (
        "seguiu sem trava e nao disse")


def test_o_reindex_mescla_em_vez_de_sobrescrever(tmp_path, monkeypatch):
    """A semantica de mesclagem do ciclo, que o fim do reindex passou a usar.

    A passagem do reindex leva minutos, e um `stamp_indexed` de outra thread
    nesse meio era perdido pelo `_save_index_state(state)` que havia ali.

    **Este teste NAO pega essa regressao**, e isso foi conferido por mutacao:
    voltar o fim do `reindex_vault` ao `_save_index_state(state)` deixa este
    teste VERDE, porque ele exercita o `_update_index_state` direto e nao a
    passagem do reindex. Exercitar a passagem de verdade exigiria uma colecao
    ChromaDB real.

    Quem pega e o `test_todo_escritor_do_estado_passa_pela_trava` abaixo, que
    derruba na mesma mutacao. Este guarda a PROPRIEDADE (o ciclo mescla em vez
    de sobrescrever); aquele guarda que o reindex passe por ele.
    """
    import delegation_core.config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "cfgdir")
    cfg = Config(vault_path=str(tmp_path / "vault"), vault_folders=["Reference"])
    (cfg.vault / "Reference").mkdir(parents=True)

    from delegation_core.vault import VaultManager
    v = VaultManager.__new__(VaultManager)
    v.cfg = cfg

    # carimbo que "outra thread" gravou durante a passagem
    notes.gravar_estado(notes.caminho_do_estado(cfg.vault),
                        {"Reference/de-outra-thread.md": 7.0})

    v._update_index_state(lambda atual: {**atual, "Reference/desta-passagem.md": 9.0})

    final = json.loads(notes.caminho_do_estado(cfg.vault).read_text(encoding="utf-8"))
    assert final == {"Reference/de-outra-thread.md": 7.0,
                     "Reference/desta-passagem.md": 9.0}, (
        f"a mesclagem perdeu um dos dois: {final}")


def test_todo_escritor_do_estado_passa_pela_trava():
    """A guarda contra o quarto sitio.

    Tres caminhos escrevem este arquivo hoje. Um quarto que chame
    `_save_index_state` direto volta a correr sem trava, e a suite ficaria
    verde: nenhum teste acima fala do caminho novo.
    """
    import inspect

    from delegation_core import vault as vault_mod

    fonte = inspect.getsource(vault_mod)
    # As unicas chamadas legitimas de `_save_index_state` sao a definicao do
    # metodo e a que existe dentro de `_update_index_state`.
    chamadas = [l.strip() for l in fonte.split("\n")
                if "_save_index_state(" in l and "def " not in l]
    assert not chamadas, (
        "escritor do estado do indice fora da trava: "
        f"{chamadas}. Use `_update_index_state`, que faz o ciclo travado")
