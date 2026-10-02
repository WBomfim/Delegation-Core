"""Nota recem-escrita precisa sair carimbada, senao o reindex a reembute inteira.

## O defeito, medido em 26/09/2026

`stamp_indexed` existia e era chamado de **um unico lugar**, o `graphbridge`,
com o comentario que descreve exatamente o problema: *"sem isto, cada uma
destas notas fica sem carimbo em `.chroma_index.json` e o proximo reindex
incremental a reembute inteira, mesmo intocada"*.

Os cinco modulos que escrevem nota, com treze chamadas de `index_note`, nunca
carimbavam. Medido no vault: **12 notas no disco sem carimbo e zero carimbo
orfao**, e as 12 eram justamente as escritas naquela noite pelo `write_note`,
pelo `vault_update_note` e pelo `export_session`.

## Por que o carimbo fica AQUI e nao dentro do `index_note`

Porque o `reindex_vault` tambem passa pelo `index_note`. Medido: carimbar custa
cerca de 5 ms, desprezivel numa escrita avulsa, mas num reindex de 10.224 notas
seriam 10.224 leituras e escritas do mesmo arquivo, cerca de **um minuto so de
serializacao**. O reindex ja carimba em lote no fim.

## Por que so em sucesso

`index_note` devolve `False` quando as linhas nao entraram no ChromaDB, e o
docstring dele explica que a funcao apaga as linhas antigas antes de escrever
as novas: numa falha, a nota pode ficar no disco sem linha nenhuma. Carimbar
assim mesmo faria todo reindex seguinte **pular justamente a nota que acabou de
se perder**.

O `reindex_vault` ja tinha essa regra, e o comentario dele diz: *"stamps a
note's mtime as current only on True; stamping unconditionally would make every
later incremental run skip the note it just lost"*. Aqui e a mesma regra no
caminho de escrita.

## O que FICOU de fora, medido e nao esquecido

Cinco chamadas de `index_note` seguem sem carimbo, quatro no `organizer` e uma
no `merger`. Nao e descuido:

- as cinco rodam **dentro do laco** do organizer sobre o `_inbox/`, que a
  propria documentacao chama de pequeno, menos de vinte arquivos por passe
- a manutencao **nao** dispara reindex; quem dispara e o hook de sessao, entao
  essas notas ficam sem carimbo ate a sessao seguinte e sao reembutidas la
- o custo disso e da ordem de vinte notas por passe de manutencao

Ou seja: e a mesma correcao, num caminho de volume menor e em cinco contextos
diferentes. Fica registrado com o numero para quem tiver margem de fazer com
calma, porque a correcao anterior, de dois sitios, ja cascateou para cinco
arquivos de teste.
"""

from __future__ import annotations

import pathlib

import pytest

from delegation_core.config import Config


class VaultFalso:
    """Acompanha a interface real do VaultManager, e nada alem dela."""

    def __init__(self, cfg, indexa_com_sucesso: bool = True):
        self.cfg = cfg
        self._ok = indexa_com_sucesso
        self.indexadas: list[str] = []
        self.carimbadas: list[str] = []
        self.apagadas: list[str] = []

    def index_note(self, content, metadata, doc_id: str = "") -> bool:
        self.indexadas.append(metadata.get("path", ""))
        return self._ok

    def stamp_indexed(self, rel_paths) -> int:
        self.carimbadas.extend(rel_paths)
        return len(rel_paths)

    def delete_notes(self, rel_paths) -> int:
        """O `rename_note` apaga as linhas antigas antes de escrever as novas.

        Faltava no duble, e o teste do rename morria com AttributeError em vez
        de medir o carimbo. Um duble que nao acompanha a interface real esconde
        (ou inventa) caminhos: e a mesma licao que ja custou cinco dubles nesta
        suite.
        """
        self.apagadas.extend(rel_paths)
        for rel in rel_paths:
            if rel in self.carimbadas:
                self.carimbadas.remove(rel)
        return len(rel_paths)

    def search(self, text, limit=5):
        return []

    @classmethod
    def note_metadata(cls, rel_path, title, folder, content=""):
        from delegation_core.vault import VaultManager
        return VaultManager.note_metadata(rel_path, title, folder)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    import delegation_core.config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "cfgdir")
    c = Config(vault_path=str(tmp_path / "vault"), vault_folders=["Reference", "Sessions"])
    (c.vault / "Reference").mkdir(parents=True)
    (c.vault / "Sessions").mkdir(parents=True)
    return c


def test_nota_nova_sai_carimbada(cfg):
    """Conferido por mutacao: tirar o `stamp_indexed` do `create_note` derruba."""
    from delegation_core.notewriter import create_note

    v = VaultFalso(cfg)
    r = create_note(v, "Reference", "Uma nota", "corpo da nota")
    assert r.get("status") == "ok", r
    assert v.carimbadas == [r["path"]], (
        f"a nota foi indexada mas nao carimbada: indexadas={v.indexadas} "
        f"carimbadas={v.carimbadas}")


def test_nota_salva_sai_carimbada(cfg):
    from delegation_core.notewriter import create_note, save_note

    v = VaultFalso(cfg)
    criada = create_note(v, "Reference", "Outra nota", "corpo")
    v.carimbadas.clear()

    r = save_note(v, criada["path"], "corpo novo")
    assert r.get("status") == "ok", r
    assert v.carimbadas == [criada["path"]]


def test_digest_de_sessao_sai_carimbado(cfg):
    """O `export_session` roda no fim de toda sessao; sem carimbo, todo digest
    e reembutido no reindex seguinte."""
    from delegation_core.session import export

    v = VaultFalso(cfg)
    r = export(v, "Um titulo", "Um resumo", "decisao a, decisao b")
    assert r.get("status") == "ok", r
    assert v.carimbadas, "o digest de sessao saiu sem carimbo"
    assert v.carimbadas[0].startswith("Sessions/")


def test_indexacao_QUE_FALHA_nao_carimba(cfg):
    """O teste que carrega o argumento.

    `index_note` apaga as linhas antigas antes de escrever as novas, entao numa
    falha a nota pode ficar no disco sem linha nenhuma. Carimbar assim mesmo
    faria todo reindex seguinte PULAR justamente a nota que acabou de se
    perder, e o estrago ficaria invisivel.

    Conferido por mutacao: carimbar sem olhar o retorno derruba este teste.
    """
    from delegation_core.notewriter import create_note

    v = VaultFalso(cfg, indexa_com_sucesso=False)
    r = create_note(v, "Reference", "Nota que falha", "corpo")

    assert r.get("status") == "ok", "a nota vai para o disco mesmo assim"
    assert v.indexadas, "a tentativa de indexar tem que acontecer"
    assert v.carimbadas == [], (
        "carimbou uma nota cuja indexacao falhou; o proximo reindex vai pular "
        "justamente a nota perdida")


def test_o_digest_que_falha_tambem_nao_carimba(cfg):
    from delegation_core.session import export

    v = VaultFalso(cfg, indexa_com_sucesso=False)
    export(v, "Titulo", "Resumo", "")
    assert v.carimbadas == []


def test_rename_carimba_a_renomeada_e_as_referentes(cfg):
    """O sexto sitio, achado pela propria operacao em 26/09/2026.

    `rename_note` indexa a nota renomeada e TODA nota cujo wikilink ele
    repontou, e nao carimbava nenhuma. Medido: um lote de 75 renomeacoes deixou
    exatamente 75 notas sem carimbo, e o reindex seguinte passou minutos
    reembutindo-as com o encoder na CPU.

    Conferido por mutacao: tirar o `stamp_indexed` do `rename_note` derruba.
    """
    from delegation_core.notewriter import create_note, rename_note

    v = VaultFalso(cfg)
    alvo = create_note(v, "Reference", "Nota alvo", "corpo")
    quem_aponta = create_note(v, "Reference", "Quem aponta",
                              f"veja [[{pathlib.Path(alvo['path']).stem}]]")
    v.carimbadas.clear()

    r = rename_note(v, alvo["path"], "Nota alvo renomeada")
    assert r.get("status") == "ok", r
    assert r["links_rewritten"] == 1, f"o wikilink nao foi repontado: {r}"

    assert r["path"] in v.carimbadas, "a nota renomeada saiu sem carimbo"
    assert quem_aponta["path"] in v.carimbadas, (
        "a nota que teve o wikilink reescrito saiu sem carimbo, e sera "
        f"reembutida no proximo reindex: carimbadas={v.carimbadas}")


def test_rename_com_indexacao_QUE_FALHA_nao_carimba(cfg):
    """A mesma regra dos outros cinco sitios: carimbar uma nota cujas linhas
    nao entraram faria o proximo reindex pular justamente a que se perdeu."""
    from delegation_core.notewriter import create_note, rename_note

    v = VaultFalso(cfg)
    alvo = create_note(v, "Reference", "Outro alvo", "corpo")
    v._ok = False
    v.carimbadas.clear()

    r = rename_note(v, alvo["path"], "Outro alvo renomeado")
    assert r.get("status") == "ok", "o arquivo e renomeado mesmo assim"
    assert v.carimbadas == [], (
        "carimbou apos indexacao que falhou; o proximo reindex pularia a nota")


def test_o_rename_carimba_em_UMA_escrita_so(cfg, monkeypatch):
    """Um rename toca ate dezenas de notas. Carimbar uma a uma reescreveria o
    arquivo de estado inteiro a cada uma."""
    from delegation_core.notewriter import create_note, rename_note

    v = VaultFalso(cfg)
    alvo = create_note(v, "Reference", "Alvo muito referenciado", "corpo")
    stem = pathlib.Path(alvo["path"]).stem
    for i in range(5):
        create_note(v, "Reference", f"Referente {i}", f"veja [[{stem}]]")

    chamadas = {"n": 0}
    real = v.stamp_indexed

    def contando(rels):
        chamadas["n"] += 1
        return real(rels)

    monkeypatch.setattr(v, "stamp_indexed", contando)
    r = rename_note(v, alvo["path"], "Alvo renomeado agora")
    assert r["links_rewritten"] == 5, r
    assert chamadas["n"] == 1, f"{chamadas['n']} escritas de estado para um rename"
