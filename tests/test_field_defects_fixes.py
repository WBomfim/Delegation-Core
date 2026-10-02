"""Unit and integration tests for fixes from the 23-Aug-2026 Field Report."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from delegation_core.config import Config
from delegation_core.extractor import (
    DatalessFileError,
    UnreadableFileError,
    extract,
    is_dataless,
)
from delegation_core.ingest import IngestManager, _paged_get
from delegation_core.vault import VaultManager


class FakeCollection:
    def __init__(self, rows=None):
        self.rows = dict(rows or {})
        self.deleted = []

    def get(self, where=None, include=None, limit=None, offset=None):
        items = [
            (i, m) for i, m in self.rows.items()
            if not where or all(m.get(k) == v for k, v in where.items())
        ]
        if offset is not None and limit is not None:
            items = items[offset:offset + limit]
        elif limit is not None:
            items = items[:limit]
        return {"ids": [i for i, _ in items], "metadatas": [m for _, m in items]}

    def delete(self, ids):
        self.deleted.extend(ids)
        for i in ids:
            self.rows.pop(i, None)

    def count(self):
        return len(self.rows)


class FakeVault:
    def __init__(self, cfg, rows=None):
        self.cfg = cfg
        self.indexed = []
        self.collection = FakeCollection(rows or {})

    def index_note(self, content, metadata, doc_id=""):
        self.indexed.append({"content": content, "metadata": metadata, "doc_id": doc_id})


    def stamp_indexed(self, rel_paths):
        # Acompanha a interface real: o VaultManager carimba o indice
        # incremental depois de indexar com sucesso. Um duble sem este metodo
        # deixaria verde um caminho que producao executa.
        self.carimbadas = getattr(self, "carimbadas", []) + list(rel_paths)
        return len(rel_paths)
    def _ensure_ready(self):
        pass


@pytest.fixture
def test_cfg(monkeypatch, tmp_path):
    import delegation_core.config as config_mod
    import delegation_core.ingest as ingest_mod
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ingest_mod, "_REGISTRY_FILE", tmp_path / "ingested_sources.json")
    v_dir = tmp_path / "vault"
    v_dir.mkdir(exist_ok=True)
    return Config(vault_path=str(v_dir))


def test_dataless_detection_and_exceptions(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("hello world", encoding="utf-8")
    assert not is_dataless(f)
    assert extract(f) == "hello world"


def test_ingest_counters_and_incremental(test_cfg, tmp_path):
    src = tmp_path / "documents"
    src.mkdir()
    (src / "normal.txt").write_text("Hello text", encoding="utf-8")
    (src / "empty.txt").write_text("", encoding="utf-8")

    vault = FakeVault(test_cfg)
    mgr = IngestManager(vault)

    # First ingest
    res1 = mgr.ingest(str(src))
    assert res1["indexed"] == 1
    assert res1["skipped_empty"] == 1
    assert res1["skipped_unchanged"] == 0
    assert len(vault.indexed) == 1

    # Second ingest: unchanged normal.txt should be skipped incrementally.
    # `indexed` counts what was actually embedded on THIS run, so it drops to 0
    # while skipped_unchanged accounts for the file. Counting the skip as an
    # index made the reported number indistinguishable between "re-embedded N"
    # and "embedded nothing, N were already present" — and the second is the
    # case an operator running reindex for recovery has to be able to see.
    vault.indexed.clear()
    res2 = mgr.ingest(str(src))
    assert res2["indexed"] == 0
    assert res2["skipped_unchanged"] == 1
    assert len(vault.indexed) == 0  # no re-embedding!
    assert res2["indexed"] == len(vault.indexed), (
        "the reported count must match what was actually embedded"
    )

    # force=True bypasses the cache — the recovery path the reindex command uses.
    vault.indexed.clear()
    res3 = mgr.ingest(str(src), force=True)
    assert res3["indexed"] == 1
    assert res3["skipped_unchanged"] == 0
    assert len(vault.indexed) == 1


def test_forget_strict_path_containment(test_cfg, tmp_path):
    work_dir = tmp_path / "Work"
    work_old = tmp_path / "Work-Old"
    work_dir.mkdir()
    work_old.mkdir()

    w_file = work_dir / "file1.txt"
    wo_file = work_old / "file2.txt"

    vault = FakeVault(test_cfg, rows={
        str(w_file): {"is_external": "true", "source_folder": str(work_dir), "path": str(w_file)},
        str(wo_file): {"is_external": "true", "source_folder": str(work_old), "path": str(wo_file)},
    })

    mgr = IngestManager(vault)
    # Forget only work_dir
    res = mgr.forget(str(work_dir))
    assert res["removed_chunks"] == 1
    # Work-Old file must survive!
    assert str(wo_file) in vault.collection.rows
    assert str(w_file) not in vault.collection.rows


def test_subkind_classification_and_weighting(test_cfg):
    # Test subkind classification
    assert VaultManager.classify_subkind("Decisions/arch.md", "Some notes") == "curated"
    assert VaultManager.classify_subkind("Sessions/transcript-123.md", "session log") == "transcript"
    assert VaultManager.classify_subkind("Sessions/2026-08-10.md", "type: session-transcript\n...") == "transcript"
    assert VaultManager.classify_subkind("Projects/chat-export.md", "source: claude-ai-export\n...") == "chat"
    assert VaultManager.classify_subkind("Projects/graphs/mygraph/article.md", "wiki") == "generated"


def test_ghost_row_resilience(test_cfg):
    # Mock collection returning None metadata
    vault = VaultManager(test_cfg)
    mock_col = MagicMock()
    mock_col.query.return_value = {
        "documents": [["Ghost content", "Good content"]],
        "metadatas": [[None, {"title": "Real Note", "path": "Decisions/real.md", "kind": "note", "subkind": "curated"}]],
        "distances": [[0.1, 0.1]],
    }
    vault.collection = mock_col
    vault._initialized = True

    hits = vault.search("query")
    # Must not raise 'NoneType' object has no attribute 'get' and should gracefully return the valid hit
    assert len(hits) == 1
    assert hits[0]["title"] == "Real Note"
