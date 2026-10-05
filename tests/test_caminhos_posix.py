"""Caminho de nota do vault e sempre POSIX, em toda plataforma.

O CI no Windows (03/10/2026) mostrou o vault gravando `Notes\\a.md` em uns
lugares e procurando `Notes/a.md` em outros: o carimbo de indexacao nao batia
(reindex reembutia a toa), `create_note` devolvia caminho com barra invertida
e a busca por titulo nao achava a nota. A causa era `str(p.relative_to(vault))`,
que no Windows usa "\\". Agora o caminho relativo sai de `.as_posix()`.

Um indice ja existente no Windows tem linhas com "\\" no id. O arquivo delas
existe, entao a varredura de orfaos as preservava, e o reindex com o id novo
duplicaria cada nota na busca. A varredura passa a tratar id de nota do vault
com "\\" como antigo.
"""

from __future__ import annotations

from delegation_core import gpu
from delegation_core.config import Config
from delegation_core.notewriter import create_note
from delegation_core.vault import VaultManager


class _Embedder:
    def __call__(self, input):
        return [[1.0] + [0.0] * 7 for _ in input]

    def embed_documents(self, input):
        return self(input)

    def embed_query(self, input):
        return self([input])[0] if isinstance(input, str) else self(list(input))

    def name(self):
        return "posix"


def _vault(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu, "take", lambda *a, **k: 0)
    cfg = Config(vault_path=str(tmp_path / "vault"), vault_folders=["Notes"],
                 search_threshold=0.0, synthesis_enabled=False)
    (tmp_path / "vault" / "Notes" / "Sub").mkdir(parents=True)
    vm = VaultManager(cfg)
    vm.ef = _Embedder()
    vm._init()
    return cfg, vm


def test_nota_em_subpasta_e_indexada_com_barra(tmp_path, monkeypatch):
    cfg, vm = _vault(tmp_path, monkeypatch)
    (cfg.vault / "Notes" / "Sub" / "n.md").write_text("# n\n\ncorpo\n", encoding="utf-8")
    vm.reindex_vault(force=True)
    ids = vm.collection.get()["ids"]
    assert any(i.startswith("Notes/Sub/n.md") for i in ids), ids
    assert not any("\\" in i for i in ids), ids
    vm._close_client()


def test_create_note_devolve_caminho_com_barra(tmp_path, monkeypatch):
    cfg, vm = _vault(tmp_path, monkeypatch)
    r = create_note(vm, "Notes", "Uma nota", "corpo")
    assert "\\" not in r["path"] and r["path"].startswith("Notes/"), r
    vm._close_client()


def test_id_antigo_com_barra_invertida_sai_no_reindex(tmp_path, monkeypatch):
    cfg, vm = _vault(tmp_path, monkeypatch)
    (cfg.vault / "Notes" / "a.md").write_text("# a\n\ncorpo\n", encoding="utf-8")
    vm.collection.add(ids=["Notes\\a.md"], documents=["corpo"],
                      metadatas=[{"path": "Notes\\a.md", "kind": "note"}])
    vm.reindex_vault(force=True)
    ids = vm.collection.get()["ids"]
    assert "Notes\\a.md" not in ids, "a linha antiga duplicaria a nota na busca"
    assert any(i.startswith("Notes/a.md") for i in ids), ids
    vm._close_client()
