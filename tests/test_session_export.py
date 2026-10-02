"""export_session e a ferramenta que o protocolo manda chamar ao fim de toda
sessao, e nao tinha um unico teste.

Medido em 03/09/2026: dos 38 modulos do nucleo, NOVE nao eram importados por
nenhum teste, somando 2.241 linhas. `session.py` era um deles, e e o mais
exercitado em producao dos nove.

Escrever estes testes achou um defeito real: `export` montava o bloco de
frontmatter a mao em vez de usar `compose_note`, entao nao recebia o alias que
`compose_note` gera quando `safe_filename` trunca o nome do arquivo. Conferido
contra o vault real: DEZ notas de Sessions tem titulo maior que o proprio stem
e NENHUMA tem alias. A maior tem 95 caracteres de titulo contra 49 de arquivo.
Todo wikilink escrito com o titulo real dessas notas resolve para nada.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from delegation_core import session
from delegation_core.config import Config
from delegation_core.linker import frontmatter_aliases

TITULO_LONGO = ("Paperclip web - primeira medicao contra a VPS e a doc oficial "
                "contradizendo a leitura de codigo")


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
                 vault_folders=["Projects", "Sessions", "Decisions"])
    (tmp_path / "Sessions").mkdir(parents=True, exist_ok=True)
    return _VaultFalso(cfg)


def _conteudo(vault, resultado) -> str:
    return (vault.cfg.vault / resultado["folder"] / resultado["path"]).read_text(
        encoding="utf-8")


# ── o basico, que nao tinha teste nenhum ────────────────────────────────────

def test_escreve_na_pasta_de_sessoes(vault):
    r = session.export(vault, "Uma sessao", "Foi feito algo.")
    assert r["status"] == "ok"
    assert r["folder"] == "Sessions"
    assert (vault.cfg.vault / "Sessions" / r["path"]).is_file()


def test_a_pasta_e_resolvida_sem_diferenciar_maiusculas(vault):
    """O comentario no codigo diz que isto ja arquivou toda sessao em Projects/."""
    assert session.export(vault, "x", "y")["folder"] == "Sessions"


def test_cai_na_primeira_pasta_quando_nao_ha_sessions(tmp_path):
    cfg = Config(vault_path=str(tmp_path), vault_folders=["Projects", "Decisions"])
    (tmp_path / "Projects").mkdir(parents=True)
    v = _VaultFalso(cfg)
    assert session.export(v, "x", "y")["folder"] == "Projects"


def test_indexa_o_que_gravou(vault):
    r = session.export(vault, "Uma sessao", "Foi feito algo.")
    assert len(vault.indexado) == 1
    conteudo, meta = vault.indexado[0]
    assert conteudo == _conteudo(vault, r)
    assert meta["path"] == f"Sessions/{r['path']}"
    assert meta["type"] == "session"


def test_duas_sessoes_com_o_mesmo_titulo_no_mesmo_dia_nao_se_apagam(vault):
    a = session.export(vault, "Mesmo titulo", "primeira")
    b = session.export(vault, "Mesmo titulo", "segunda")
    assert a["path"] != b["path"]
    assert "primeira" in _conteudo(vault, a)
    assert "segunda" in _conteudo(vault, b)


def test_decisoes_viram_lista(vault):
    r = session.export(vault, "s", "resumo", key_decisions="uma, outra , terceira")
    texto = _conteudo(vault, r)
    assert "## Key decisions / artifacts" in texto
    assert "- uma" in texto and "- outra" in texto and "- terceira" in texto


def test_sem_decisoes_a_secao_nao_aparece(vault):
    assert "Key decisions" not in _conteudo(vault, session.export(vault, "s", "r"))


def test_decisoes_vazias_nao_viram_marcador_solto(vault):
    texto = _conteudo(vault, session.export(vault, "s", "r", key_decisions=" , , "))
    assert "Key decisions" not in texto
    assert "\n- \n" not in texto


# ── o defeito que escrever estes testes achou ───────────────────────────────

def test_titulo_longo_ganha_alias_para_o_link_resolver(vault):
    """O defeito. export montava o frontmatter a mao e pulava o alias.

    Sem o alias, um wikilink escrito com o titulo real da sessao nao resolve,
    porque o nome do arquivo foi truncado em 50 caracteres.
    """
    from delegation_core.vault import safe_filename

    r = session.export(vault, TITULO_LONGO, "resumo")
    assert len(safe_filename(TITULO_LONGO)) < len(TITULO_LONGO), "premissa mudou"

    aliases = {a.lower() for a in frontmatter_aliases(_conteudo(vault, r))}
    assert TITULO_LONGO.lower() in aliases, (
        "a nota de sessao nao pode ser linkada pelo proprio titulo"
    )


def test_o_alias_cobre_tambem_a_forma_com_data(vault):
    """Links reais neste vault usam `[[2026-09-03-Titulo completo]]`."""
    import datetime as _dt

    r = session.export(vault, TITULO_LONGO, "resumo")
    hoje = _dt.datetime.now().strftime("%Y-%m-%d")
    aliases = {a.lower() for a in frontmatter_aliases(_conteudo(vault, r))}
    assert f"{hoje}-{TITULO_LONGO}".lower() in aliases


def test_titulo_curto_nao_ganha_alias_redundante(vault):
    r = session.export(vault, "Curto", "resumo")
    assert not frontmatter_aliases(_conteudo(vault, r))


# ── um unico bloco de frontmatter ───────────────────────────────────────────

def test_um_unico_bloco_de_frontmatter(vault):
    """Concatenar bloco proprio com o gerado ja produziu dois blocos empilhados,
    e o Obsidian le so o primeiro."""
    texto = _conteudo(vault, session.export(vault, TITULO_LONGO, "resumo"))
    assert texto.count("\n---\n") == 1, f"blocos empilhados:\n{texto[:400]}"
    assert texto.startswith("---\n")


def test_o_type_session_sobrevive_a_fusao(vault):
    texto = _conteudo(vault, session.export(vault, "s", "r"))
    fm = texto[4:texto.index("\n---\n", 4)]
    assert "type: session" in fm
    assert "ai_generated: true" in fm
    assert "date: " in fm


def test_titulo_com_aspas_nao_quebra_o_yaml(vault):
    r = session.export(vault, 'Sessao com "aspas" e: dois pontos', "resumo")
    texto = _conteudo(vault, r)
    fm = texto[4:texto.index("\n---\n", 4)]
    linha = next(l for l in fm.split("\n") if l.startswith("title:"))
    assert linha.startswith('title: "') and linha.endswith('"')


def test_o_corpo_carrega_titulo_resumo_e_hora(vault):
    r = session.export(vault, "Uma sessao", "  Foi feito algo.  ")
    texto = _conteudo(vault, r)
    assert "# Uma sessao" in texto
    assert "## Summary" in texto
    assert "Foi feito algo." in texto
