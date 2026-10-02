"""notewriter.py — the one path a note takes into the vault.

Extracted when the dashboard gained editing. The alternative was letting the
dashboard write and index on its own, which would have created a second write
path free to drift from the MCP tools' — the failure this codebase has spent a
day removing (two wikilink parsers, two definitions of "generated", a labeler
nothing called). server.py's write_note now delegates here, so these tests cover
both surfaces at once.
"""

import pytest

from delegation_core import notewriter
from delegation_core.config import Config


class FakeVault:
    """VaultManager stand-in: records indexing, no BGE/ChromaDB."""

    def __init__(self, cfg, hits=None):
        self.cfg = cfg
        self.indexed = []
        self._hits = hits or []

    def index_note(self, content, meta):
        self.indexed.append((content, meta))


    def stamp_indexed(self, rel_paths):
        # Acompanha a interface real: o VaultManager carimba o indice
        # incremental depois de indexar com sucesso. Um duble sem este metodo
        # deixaria verde um caminho que producao executa.
        self.carimbadas = getattr(self, "carimbadas", []) + list(rel_paths)
        return len(rel_paths)
    def search(self, text, limit=5):
        return self._hits


@pytest.fixture
def vault(tmp_path):
    cfg = Config(vault_path=str(tmp_path), vault_folders=["Reference", "Decisions"])
    (tmp_path / "Reference").mkdir(parents=True)
    return FakeVault(cfg)


def test_create_note_writes_and_indexes(vault):
    result = notewriter.create_note(vault, "Reference", "My Note", "## Body\ntext")
    assert result["status"] == "ok"
    disk = (vault.cfg.vault / result["path"]).read_text(encoding="utf-8")
    assert 'title: "My Note"' in disk
    assert "## Body" in disk
    assert vault.indexed[0][1]["path"] == result["path"]


def test_create_note_emits_exactly_one_frontmatter_block(vault):
    """A caller passing its own frontmatter used to get a second block stacked
    under the generated one, silently turning its keys into body text."""
    result = notewriter.create_note(
        vault, "Reference", "Titled", '---\nsubtitle: "mine"\n---\n\n## Body\n')
    disk = (vault.cfg.vault / result["path"]).read_text(encoding="utf-8")
    assert disk.count("\n---\n") == 1
    assert 'subtitle: "mine"' in disk
    assert 'title: "Titled"' in disk


def test_create_note_rejects_an_unconfigured_folder(vault):
    assert "error" in notewriter.create_note(vault, "Nope", "t", "c")


def test_create_note_requires_a_title(vault):
    assert "error" in notewriter.create_note(vault, "Reference", "   ", "c")


def test_create_note_does_not_overwrite_a_same_day_same_title_note(vault):
    """That collision used to destroy the first note's file and its index row."""
    a = notewriter.create_note(vault, "Reference", "Same", "first")
    b = notewriter.create_note(vault, "Reference", "Same", "second")
    assert a["path"] != b["path"]
    assert "first" in (vault.cfg.vault / a["path"]).read_text(encoding="utf-8")
    assert "second" in (vault.cfg.vault / b["path"]).read_text(encoding="utf-8")


def test_save_note_overwrites_verbatim_and_reindexes(vault):
    created = notewriter.create_note(vault, "Reference", "Edit Me", "original")
    vault.indexed.clear()

    body = '---\ntitle: "Edit Me"\n---\n\nedited by hand\n'
    result = notewriter.save_note(vault, created["path"], body)

    assert result["status"] == "ok"
    assert (vault.cfg.vault / created["path"]).read_text(encoding="utf-8") == body
    assert vault.indexed[0][1]["path"] == created["path"]


def test_save_note_does_not_append_a_related_block(vault):
    """create injects wikilinks; save must not, or every save would edit the
    user's text behind them."""
    created = notewriter.create_note(vault, "Reference", "Stable", "body")
    body = "just this\n"
    notewriter.save_note(vault, created["path"], body)
    assert (vault.cfg.vault / created["path"]).read_text(encoding="utf-8") == body


def test_save_note_refuses_a_path_outside_the_vault(vault):
    assert "error" in notewriter.save_note(vault, "../../etc/passwd", "x")


def test_save_note_refuses_a_missing_note(vault):
    assert "error" in notewriter.save_note(vault, "Reference/ghost.md", "x")


def test_save_note_refuses_a_non_markdown_file(vault):
    other = vault.cfg.vault / "Reference" / "data.txt"
    other.write_text("x", encoding="utf-8")
    assert "error" in notewriter.save_note(vault, "Reference/data.txt", "y")


def test_create_note_resolves_case_insensitive_folder(vault):
    # configured folder is "Reference"
    res1 = notewriter.create_note(vault, "reference", "Lower", "content")
    assert res1["status"] == "ok"
    assert res1["folder"] == "Reference"
    assert res1["path"].startswith("Reference/")

    res2 = notewriter.create_note(vault, "DECISIONS", "Upper", "content")
    assert res2["status"] == "ok"
    assert res2["folder"] == "Decisions"
    assert res2["path"].startswith("Decisions/")


def test_resolve_vault_folder():
    from delegation_core.vault import resolve_vault_folder
    cfg = Config(vault_folders=["Projects", "Decisions", "weekly-reports"])
    assert resolve_vault_folder(cfg, "projects") == "Projects"
    assert resolve_vault_folder(cfg, "PROJECTS") == "Projects"
    assert resolve_vault_folder(cfg, "Decisions/") == "Decisions"
    assert resolve_vault_folder(cfg, "WEEKLY-REPORTS") == "weekly-reports"
    assert resolve_vault_folder(cfg, "nonexistent") is None
    assert resolve_vault_folder(cfg, None) is None
    assert resolve_vault_folder(cfg, "") is None

