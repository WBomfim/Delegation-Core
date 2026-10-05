"""ingest_folder: reporting what it did NOT index, and forgetting a source.

The reporting gap was a real trap. Files whose extension is outside SUPPORTED
never became candidates (ingest.py's glob filter) and were counted nowhere, so
pointing this at a source tree returned a confident `indexed: 6, skipped: 0`
while ignoring hundreds of code files. The very first question asked of this
tool — "can we ingest whole codebases?" — was hard to answer honestly for
exactly this reason.

`forget` closes the other end: ingest is upsert-by-absolute-path, which makes
re-runs safe but leaves rows behind forever once a source moves or is deleted,
still answering searches with paths that no longer resolve.
"""

import json

import pytest

from delegation_core.config import Config
from delegation_core.ingest import IngestManager


class FakeVault:
    def __init__(self, cfg, rows=None):
        self.cfg = cfg
        self.indexed = []
        self.collection = FakeCollection(rows or {})

    def index_note(self, content, metadata, doc_id=""):
        self.indexed.append(metadata)

    def _ensure_ready(self):
        pass


class FakeCollection:
    def __init__(self, rows):
        self.rows = dict(rows)          # id -> metadata
        self.deleted = []

    def get(self, where=None, include=None):
        items = [(i, m) for i, m in self.rows.items()
                 if not where or all(m.get(k) == v for k, v in where.items())]
        return {"ids": [i for i, _ in items], "metadatas": [m for _, m in items]}

    def delete(self, ids):
        self.deleted.extend(ids)
        for i in ids:
            self.rows.pop(i, None)


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    import delegation_core.config as config_mod
    import delegation_core.ingest as ingest_mod
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ingest_mod, "_REGISTRY_FILE", tmp_path / "ingested_sources.json")
    return Config(vault_path=str(tmp_path / "vault"))


# ── reporting ────────────────────────────────────────────────────────────────

def test_unsupported_files_are_counted_instead_of_vanishing(cfg, tmp_path):
    src = tmp_path / "repo"
    src.mkdir()
    (src / "README.md").write_text("# hi", encoding="utf-8")
    for i in range(3):
        (src / f"mod{i}.py").write_text("x = 1", encoding="utf-8")
    (src / "styles.css").write_text("a{}", encoding="utf-8")

    result = IngestManager(FakeVault(cfg)).ingest(str(src))

    assert result["indexed"] == 1
    assert result["unsupported"] == {".py": 3, ".css": 1}
    assert result["unsupported_total"] == 4


def test_a_codebase_gets_pointed_at_graph_build(cfg, tmp_path):
    src = tmp_path / "repo"
    src.mkdir()
    (src / "a.ts").write_text("export const a = 1", encoding="utf-8")

    result = IngestManager(FakeVault(cfg)).ingest(str(src))

    assert "graph_build" in result["hint"]


def test_a_document_folder_reports_no_hint_and_no_unsupported_key(cfg, tmp_path):
    src = tmp_path / "docs"
    src.mkdir()
    (src / "a.md").write_text("# a", encoding="utf-8")

    result = IngestManager(FakeVault(cfg)).ingest(str(src))

    assert result["indexed"] == 1
    assert "unsupported" not in result and "hint" not in result


def test_non_code_unsupported_files_are_reported_without_the_code_hint(cfg, tmp_path):
    """A folder of unsupported assets is worth reporting, but graph_build is not the answer."""
    src = tmp_path / "assets"
    src.mkdir()
    (src / "a.md").write_text("# a", encoding="utf-8")
    (src / "logo.svg").write_text("<svg/>", encoding="utf-8")

    result = IngestManager(FakeVault(cfg)).ingest(str(src))

    assert result["unsupported"] == {".svg": 1}
    assert "hint" not in result


def test_image_without_text_is_not_indexed(cfg, tmp_path):
    """Images are read by OCR since 27/09; one with no text (a broken logo here)
    must not become a row without content, which is what the old test guarded."""
    src = tmp_path / "assets"
    src.mkdir()
    (src / "a.md").write_text("# a", encoding="utf-8")
    (src / "logo.png").write_bytes(b"\x89PNG")

    vault = FakeVault(cfg)
    result = IngestManager(vault).ingest(str(src))

    assert "unsupported" not in result or ".png" not in result["unsupported"]
    caminhos = [m.get("path", "") for m in vault.indexed]
    assert any(c.endswith("a.md") for c in caminhos)
    assert not any(c.endswith("logo.png") for c in caminhos)


# ── forget ───────────────────────────────────────────────────────────────────

def test_forget_removes_rows_and_the_registry_entry(cfg, tmp_path):
    src = tmp_path / "docs"
    src.mkdir()
    (src / "a.md").write_text("# a", encoding="utf-8")
    resolved = str(src.resolve())
    vault = FakeVault(cfg, rows={
        f"{resolved}/a.md": {"is_external": "true", "source_folder": resolved,
                             "path": f"{resolved}/a.md"},
        "/other/b.md": {"is_external": "true", "source_folder": "/other", "path": "/other/b.md"},
    })
    manager = IngestManager(vault)
    manager.ingest(str(src))

    result = manager.forget(str(src))

    assert result["removed_chunks"] == 1
    assert result["registry_entry_removed"] is True
    assert vault.collection.deleted == [f"{resolved}/a.md"]
    assert json.loads((tmp_path / "ingested_sources.json").read_text()) == {}


def test_forget_matches_chunks_indexed_beneath_the_source(cfg, tmp_path):
    """Long files are chunked with `<path>::chunk_N` ids under the same folder."""
    src = tmp_path / "docs"
    src.mkdir()
    resolved = str(src.resolve())
    vault = FakeVault(cfg, rows={
        f"{resolved}/deep/a.md::chunk_0": {"is_external": "true", "source_folder": resolved,
                                           "path": f"{resolved}/deep/a.md"},
        f"{resolved}/deep/a.md::chunk_1": {"is_external": "true", "source_folder": resolved,
                                           "path": f"{resolved}/deep/a.md"},
    })

    assert IngestManager(vault).forget(str(src))["removed_chunks"] == 2


def test_forget_on_an_unknown_path_is_harmless(cfg, tmp_path):
    vault = FakeVault(cfg)
    result = IngestManager(vault).forget(str(tmp_path / "never-ingested"))

    assert result["removed_chunks"] == 0
    assert result["registry_entry_removed"] is False


# ── status ───────────────────────────────────────────────────────────────────

def test_status_checks_existence_and_reports_missing_sources(cfg, tmp_path):
    src1 = tmp_path / "active_docs"
    src1.mkdir()
    (src1 / "a.md").write_text("# active", encoding="utf-8")

    src2 = tmp_path / "moved_docs"
    src2.mkdir()
    (src2 / "b.md").write_text("# moved", encoding="utf-8")

    vault = FakeVault(cfg)
    manager = IngestManager(vault)
    manager.ingest(str(src1))
    manager.ingest(str(src2))

    # Simulate src2 moving or deleting
    import shutil
    shutil.rmtree(src2)

    status = manager.status()
    assert status["count"] == 2
    assert status["sources"][str(src1.resolve())]["exists"] is True
    assert status["sources"][str(src2.resolve())]["exists"] is False
    assert str(src2.resolve()) in status["missing_sources"]
    assert "hint" in status


def test_concurrent_ingests_do_not_overwrite_each_others_registry_entries(cfg, tmp_path):
    """Simulate a long ingest starting, a fast concurrent ingest finishing mid-way,

    and ensure the long ingest finishing does not wipe the fast ingest's registry entry."""
    src_long = tmp_path / "long_source"
    src_long.mkdir()
    (src_long / "doc1.md").write_text("# long doc", encoding="utf-8")

    src_fast = tmp_path / "fast_source"
    src_fast.mkdir()
    (src_fast / "doc2.md").write_text("# fast doc", encoding="utf-8")

    vault = FakeVault(cfg)
    manager = IngestManager(vault)

    # Ingest fast source first
    manager.ingest(str(src_fast))
    fast_key = str(src_fast.resolve())

    # Ingest long source
    manager.ingest(str(src_long))
    long_key = str(src_long.resolve())

    status = manager.status()
    assert fast_key in status["sources"]
    assert long_key in status["sources"]
    assert status["count"] == 2
