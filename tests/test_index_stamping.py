"""Nota escrita fora do reindex tem que ficar carimbada, ou o reindex a refaz.

`reindex_vault(force=False)` pula a nota cujo mtime bate com o valor gravado em
`.chroma_index.json`. Ate 02/09/2026 os unicos escritores desse arquivo eram
`reindex_vault` e `delete_notes`. Todo o resto que indexa nota deixava sem
carimbo, e o proximo reindex "incremental" reembutia nota ja indexada e
intocada.

Medido neste vault: pasta Reference com 8.262 notas no disco e 3.469
carimbadas. graph_build escreve o relatorio mais um artigo por comunidade
direto por `index_note`, aos milhares, e nunca carimba nenhum. Resultado:
4.793 notas geradas reembutidas em CADA reindex incremental, o que transforma
um run com cinco notas realmente novas numa reconstrucao quase completa,
segurando a GPU por volta de vinte e cinco minutos.

O custo nao e academico: o hook de SessionEnd dispara `delegation-core reindex`
ao fim de toda sessao do Claude Code.
"""
from __future__ import annotations

import json

import pytest

from delegation_core.config import Config
from delegation_core.vault import VaultManager


@pytest.fixture
def vault(tmp_path):
    cfg = Config(vault_path=str(tmp_path), vault_folders=["Reference", "Sessions"])
    (tmp_path / "Reference").mkdir(parents=True, exist_ok=True)
    (tmp_path / "Sessions").mkdir(parents=True, exist_ok=True)
    v = VaultManager(cfg)
    v._ensure_ready = lambda: None
    return v


def _escreve(vault, rel: str, texto: str = "# nota\n") -> str:
    caminho = vault.cfg.vault / rel
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(texto, encoding="utf-8")
    return rel


def test_carimba_o_mtime_do_arquivo(vault):
    rel = _escreve(vault, "Reference/a.md")

    assert vault.stamp_indexed([rel]) == 1

    estado = vault._load_index_state()
    esperado = (vault.cfg.vault / rel).stat().st_mtime
    assert estado[rel] == pytest.approx(esperado)


def test_carimbo_faz_o_reindex_pular(vault):
    """A propriedade que importa: o carimbo tem que ser lido pelo reindex.

    Sem isto o teste acima prova so que um numero foi gravado num arquivo, e
    nao que ele muda o comportamento de quem o le.
    """
    rel = _escreve(vault, "Reference/a.md")
    vault.stamp_indexed([rel])

    estado = vault._load_index_state()
    mtime = (vault.cfg.vault / rel).stat().st_mtime
    # A condicao exata de reindex_vault.
    assert abs(estado.get(rel, 0) - mtime) < 0.001


def test_arquivo_modificado_deixa_de_bater(vault):
    """Carimbo nao pode congelar uma nota fora do indice para sempre."""
    import os
    import time

    rel = _escreve(vault, "Reference/a.md")
    vault.stamp_indexed([rel])
    estado_antes = vault._load_index_state()

    time.sleep(0.01)
    caminho = vault.cfg.vault / rel
    caminho.write_text("# mudou\n", encoding="utf-8")
    os.utime(caminho, (time.time() + 5, time.time() + 5))

    novo_mtime = caminho.stat().st_mtime
    assert abs(estado_antes[rel] - novo_mtime) >= 0.001, \
        "a nota editada continuaria sendo pulada pelo reindex"


def test_lote_faz_uma_unica_escrita(vault, monkeypatch):
    """Carimbar dentro de index_note reescreveria o arquivo inteiro por nota.

    Num reindex de 8.581 notas isso e 8.581 escritas de arquivo cheio. O metodo
    e em lote justamente por isso, e este teste e o que impede alguem de
    "simplificar" movendo-o para dentro do laco.
    """
    rels = [_escreve(vault, f"Reference/n{i}.md") for i in range(50)]
    escritas = {"n": 0}

    # Espiona `notes.gravar_estado`, que passou a ser o escritor quando a posse
    # do arquivo de estado saiu do `vault.py` para o ciclo travado. Espionar o
    # metodo antigo deixava este teste contar ZERO escritas e ficar verde por
    # nao olhar mais nada.
    from delegation_core import notes as notes_mod
    real = notes_mod.gravar_estado

    def contando(caminho, estado):
        escritas["n"] += 1
        real(caminho, estado)

    monkeypatch.setattr(notes_mod, "gravar_estado", contando)
    assert vault.stamp_indexed(rels) == 50
    assert escritas["n"] == 1, f"{escritas['n']} escritas para um lote de 50"


def test_lista_vazia_nao_escreve_nada(vault, monkeypatch):
    """Tambem reapontado: espionar o metodo antigo tornaria este teste vacuo.

    `stamp_indexed` nem chega ao escritor com lista vazia, entao um espiao no
    lugar errado nunca dispararia e o teste passaria sem olhar nada. Com o
    espiao no escritor de verdade ele volta a afirmar alguma coisa, e pega de
    quebra a trava sendo tomada a toa para um lote vazio.
    """
    from delegation_core import notes as notes_mod

    def explode(caminho, estado):
        raise AssertionError("escreveu o estado para um lote vazio")

    monkeypatch.setattr(notes_mod, "gravar_estado", explode)
    assert vault.stamp_indexed([]) == 0


def test_arquivo_inexistente_nao_e_carimbado(vault):
    """Carimbar mtime de arquivo que nao existe faria o proximo reindex pular
    uma nota que ele precisa colher como orfa."""
    rel = _escreve(vault, "Reference/existe.md")

    assert vault.stamp_indexed([rel, "Reference/nao_existe.md"]) == 1

    estado = vault._load_index_state()
    assert rel in estado
    assert "Reference/nao_existe.md" not in estado


def test_carimbo_preserva_o_que_ja_estava(vault):
    """Uma escrita de lote nao pode apagar o estado das outras notas."""
    a = _escreve(vault, "Reference/a.md")
    vault.stamp_indexed([a])
    b = _escreve(vault, "Sessions/b.md")
    vault.stamp_indexed([b])

    estado = vault._load_index_state()
    assert set(estado) >= {a, b}


def test_estado_gravado_e_json_legivel(vault):
    rel = _escreve(vault, "Reference/a.md")
    vault.stamp_indexed([rel])
    do_disco = json.loads(vault._index_state_path().read_text(encoding="utf-8"))
    assert rel in do_disco


# ── o chamador que motivou tudo ─────────────────────────────────────────────

def test_graphbridge_carimba_o_que_arquivou(tmp_path, monkeypatch):
    """graph_build escrevia milhares de artigos e nao carimbava nenhum."""
    from delegation_core import graphbridge

    cfg = Config(vault_path=str(tmp_path), vault_folders=["Reference"])

    class _VaultFalso:
        def __init__(self):
            self.cfg = cfg
            self.carimbados: list[str] = []

        def index_note(self, *a, **kw):
            return True

        def note_metadata(self, rel, title, folder, content=""):
            return {"path": rel, "title": title, "folder": folder}

        def search(self, *a, **kw):
            return []

        def stamp_indexed(self, rels):
            self.carimbados.extend(rels)
            return len(rels)

        def delete_notes(self, rels):
            return len(rels)

    wiki = tmp_path / "wiki"
    wiki.mkdir()
    for i in range(3):
        (wiki / f"artigo{i}.md").write_text(f"# artigo {i}\n", encoding="utf-8")

    vf = _VaultFalso()
    resultado = graphbridge._write_artifacts_to_vault(
        vf, "grafo-teste", "# relatorio\n", wiki, [])

    assert vf.carimbados, "graphbridge arquivou notas e nao carimbou nenhuma"
    assert set(vf.carimbados) == set(resultado["written_paths"])
    assert len(vf.carimbados) == 4          # 1 relatorio + 3 artigos
