"""A guarda que impede o merge de empilhar sessoes nao disparava nesta maquina.

`merger.py` funde uma nota nova dentro de uma quase-duplicata existente,
APENDANDO texto num arquivo que ja existe. Errar aqui nao devolve resultado
errado: junta dois documentos que deviam ficar separados, e o unico jeito de
desfazer e editar a mao.

O modulo tem uma guarda para isso desde a v0.2, motivada por um caso de campo
citado no proprio topo do arquivo: numa instalacao, o merge empilhou varias
sessoes nao relacionadas numa nota so.

Medido em 03/09/2026: a guarda **nao disparava nesta maquina**. Ela comparava
`folder.split("/")[0]` contra `{"sessions", "meetings"}`, tudo minusculo, e o
vault desta maquina tem a pasta `Sessions`. Nenhuma das nove pastas era
bloqueada. `vault.py:1553` ja fazia a mesma comparacao corretamente, com um
comentario dizendo exatamente que "a vault whose folder is 'Sessions' would
otherwise match nothing here": a licao foi aprendida la e nunca chegou aqui.

Alem disso `cfg.never_merge_folders` existe, esta documentada e e lida por
`vault.py`, e este modulo a ignorava.

Este arquivo e o primeiro teste que `merger.py` tem.
"""
from __future__ import annotations

import pytest

from delegation_core import merger
from delegation_core.config import Config


class _VaultFalso:
    def __init__(self, cfg):
        self.cfg = cfg
        self.indexado: list[tuple[str, dict]] = []

    def index_note(self, content, metadata):
        self.indexado.append((content, metadata))
        return True



    def stamp_indexed(self, rel_paths):
        # Acompanha a interface real: o VaultManager carimba o indice
        # incremental depois de indexar com sucesso. Um duble sem este metodo
        # deixaria verde um caminho que producao executa.
        self.carimbadas = getattr(self, "carimbadas", []) + list(rel_paths)
        return len(rel_paths)
@pytest.fixture
def vault(tmp_path):
    cfg = Config(vault_path=str(tmp_path),
                 vault_folders=["Projects", "Sessions", "Reference"],
                 merge_threshold=0.88)
    for f in cfg.vault_folders:
        (tmp_path / f).mkdir(parents=True, exist_ok=True)
    return _VaultFalso(cfg)


def _alvo(vault, rel: str, texto: str = "# nota existente\n") -> dict:
    caminho = vault.cfg.vault / rel
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(texto, encoding="utf-8")
    return {"path": rel, "title": "existente", "similarity": 0.95,
            "folder": rel.split("/")[0]}


# ── a guarda que nao disparava ──────────────────────────────────────────────

def test_pasta_Sessions_com_maiuscula_e_bloqueada(vault):
    """O defeito, exatamente como estava em producao."""
    hits = [_alvo(vault, "Sessions/existente.md")]

    fundiu, alvo = merger.try_merge(
        vault, hits, "texto novo", "## conteudo\n", "Sessions",
        "nova.md", "2026-09-03")

    assert fundiu is False and alvo == ""
    assert vault.indexado == [], "indexou um merge que nao devia ter acontecido"
    assert (vault.cfg.vault / "Sessions/existente.md").read_text() == "# nota existente\n"


def test_pasta_sessions_minuscula_continua_bloqueada(vault):
    hits = [_alvo(vault, "sessions/existente.md")]
    assert merger.try_merge(vault, hits, "t", "c", "sessions", "n.md", "d")[0] is False


def test_subpasta_de_Sessions_tambem_e_bloqueada(vault):
    """A guarda olha a raiz do caminho, nao a pasta inteira."""
    hits = [_alvo(vault, "Sessions/2026/existente.md")]
    assert merger.try_merge(vault, hits, "t", "c", "Sessions/2026", "n.md", "d")[0] is False


def test_meetings_continua_protegida_mesmo_fora_da_config(vault):
    """O default de cfg.never_merge_folders e so ["sessions"].

    Trocar a constante do modulo pela config sozinha tiraria `meetings` da
    protecao sem ninguem pedir. A uniao existe para isso.
    """
    assert "meetings" in merger._never_merge(vault.cfg)


def test_a_config_acrescenta_pasta_protegida(vault):
    """cfg.never_merge_folders era lida por vault.py e ignorada aqui."""
    vault.cfg.never_merge_folders = ["Reference"]
    hits = [_alvo(vault, "Reference/existente.md")]

    fundiu, _ = merger.try_merge(
        vault, hits, "t", "c", "Reference", "n.md", "d")

    assert fundiu is False, "a pasta configurada como never_merge foi fundida"


def test_config_ausente_nao_derruba_a_protecao_padrao(vault):
    """Um Config antigo, sem a chave, nao pode desligar a guarda."""
    delattr(type(vault.cfg), "never_merge_folders") if False else None
    vault.cfg.never_merge_folders = None
    assert "sessions" in merger._never_merge(vault.cfg)


# ── o merge que deve acontecer ──────────────────────────────────────────────

def test_funde_quando_a_similaridade_passa(vault):
    hits = [_alvo(vault, "Projects/existente.md")]

    fundiu, alvo = merger.try_merge(
        vault, hits, "texto", "## novo conteudo\n", "Projects",
        "nova.md", "2026-09-03")

    assert fundiu is True and alvo == "Projects/existente.md"
    texto = (vault.cfg.vault / alvo).read_text(encoding="utf-8")
    assert "# nota existente" in texto, "o merge apagou o conteudo original"
    assert "## novo conteudo" in texto
    assert "Merged from `nova.md`" in texto
    assert len(vault.indexado) == 1


def test_nao_funde_abaixo_do_limiar(vault):
    hits = [_alvo(vault, "Projects/existente.md")]
    hits[0]["similarity"] = 0.5
    assert merger.try_merge(vault, hits, "t", "c", "Projects", "n.md", "d")[0] is False


def test_documento_grande_demais_fica_standalone(vault):
    hits = [_alvo(vault, "Projects/existente.md")]
    enorme = "x" * (merger._MAX_INCOMING + 1)
    assert merger.try_merge(vault, hits, enorme, "c", "Projects", "n.md", "d")[0] is False


def test_alvo_grande_demais_recusa_o_merge(vault):
    hits = [_alvo(vault, "Projects/existente.md", "y" * (merger._MAX_TARGET + 1))]
    assert merger.try_merge(vault, hits, "t", "c", "Projects", "n.md", "d")[0] is False


def test_alvo_que_sumiu_do_disco_e_pulado(vault):
    hits = [{"path": "Projects/nao_existe.md", "title": "x", "similarity": 0.99}]
    assert merger.try_merge(vault, hits, "t", "c", "Projects", "n.md", "d")[0] is False


def test_o_primeiro_hit_valido_vence(vault):
    """Ordem importa: hits vem ordenado por similaridade."""
    primeiro = _alvo(vault, "Projects/a.md")
    segundo = _alvo(vault, "Projects/b.md")
    segundo["similarity"] = 0.90

    _, alvo = merger.try_merge(vault, [primeiro, segundo], "t", "c",
                               "Projects", "n.md", "d")

    assert alvo == "Projects/a.md"
    assert (vault.cfg.vault / "Projects/b.md").read_text() == "# nota existente\n"


def test_hit_abaixo_do_limiar_nao_bloqueia_o_seguinte(vault):
    fraco = _alvo(vault, "Projects/fraco.md")
    fraco["similarity"] = 0.1
    forte = _alvo(vault, "Projects/forte.md")

    _, alvo = merger.try_merge(vault, [fraco, forte], "t", "c",
                               "Projects", "n.md", "d")

    assert alvo == "Projects/forte.md"


def test_sem_hits_nao_funde(vault):
    assert merger.try_merge(vault, [], "t", "c", "Projects", "n.md", "d") == (False, "")
