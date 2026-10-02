"""VaultManager reopens its collection when another process writes the index.

Concurrent writers are by design: the SessionEnd hook fires a detached
``reindex``, the SessionStart hook fires ``maintain``, and any CLI use writes to
the same path while the server runs. A PersistentClient loads the vector index
once and never re-reads it, and the module write lock is a threading.Lock that
knows nothing about another process.

Measured before this existed: after a CLI ingest, a running server answered
scope='all' with pre-write content and failed every scope-filtered query with
ChromaDB's "Error finding id", while a freshly opened client read the same index
perfectly. Only a restart cleared it — which made the documented "the transcript
is searchable right after the session" path the thing that broke search.
"""

import pytest

from delegation_core.config import Config
from delegation_core.vault import VaultManager


@pytest.fixture
def vm(tmp_path, monkeypatch):
    manager = VaultManager(Config(vault_path=str(tmp_path / "vault")))
    manager.cfg.chroma_path.mkdir(parents=True, exist_ok=True)
    (manager.cfg.chroma_path / "chroma.sqlite3").write_bytes(b"one")

    opens = []

    def fake_init():
        opens.append(True)
        manager.collection = object()
        manager._disk_state = manager._read_disk_state()
        manager._initialized = True

    monkeypatch.setattr(manager, "_init", fake_init)
    manager.opens = opens
    return manager


def _touch(vm, content=b"two"):
    """Simulate a foreign write: the sqlite file changes size."""
    (vm.cfg.chroma_path / "chroma.sqlite3").write_bytes(content)


def test_a_foreign_write_reopens_the_collection(vm):
    vm._ensure_ready()
    assert len(vm.opens) == 1

    _touch(vm)
    vm._ensure_ready()

    assert len(vm.opens) == 2


def test_an_unchanged_index_is_not_reopened(vm):
    vm._ensure_ready()

    for _ in range(5):
        vm._ensure_ready()

    assert len(vm.opens) == 1, "reopening on every call would reload the index constantly"


def test_our_own_write_does_not_count_as_foreign(vm, monkeypatch):
    """index_note adopts the new fingerprint, or every write triggers a reopen
    on the very next call."""
    vm._ensure_ready()

    class Collection:
        def upsert(self, ids, documents, metadatas):
            _touch(vm, b"written-by-us")

    vm.collection = Collection()
    vm.index_note("body", {"title": "t", "path": "Decisions/a.md", "folder": "Decisions"})
    vm._ensure_ready()

    assert len(vm.opens) == 1


def test_reload_is_skipped_before_the_first_open(vm):
    """Nothing to reconcile yet; _init owns the first open."""
    vm._reload_if_disk_changed()

    assert vm.opens == []


def test_a_missing_index_file_does_not_trigger_reopens(vm):
    vm._ensure_ready()
    (vm.cfg.chroma_path / "chroma.sqlite3").unlink()

    vm._ensure_ready()

    assert len(vm.opens) == 1, "an unreadable fingerprint is unknown, not changed"


def test_the_embedding_function_survives_a_reopen(tmp_path, monkeypatch):
    """Rebuilding it reloads BGE onto the GPU. A reload caused by someone else's
    write must not pay that, and on a full GPU the rebuild can fail outright."""
    built = []

    def fake_builder(model, max_seq_length=None, batch_size=None, device=None):
        # v0.12 added the two execution caps to the factory's signature. A double
        # that predates them makes _init raise, so the collection never opens and
        # this test reads "never built" as "not rebuilt" — passing vacuously.
        built.append({"model": model, "max_seq_length": max_seq_length,
                      "batch_size": batch_size})
        return object()

    monkeypatch.setattr("delegation_core.vault.make_bge_embedding_function", fake_builder)

    class FakeCollection:
        def count(self):
            return 0

    class FakeClient:
        def get_or_create_collection(self, **kwargs):
            return FakeCollection()

    import sys
    import types

    fake_chromadb = types.ModuleType("chromadb")
    fake_chromadb.Settings = lambda **kw: None
    fake_chromadb.PersistentClient = lambda **kw: FakeClient()
    monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)

    manager = VaultManager(Config(vault_path=str(tmp_path / "vault")))
    manager._init()
    assert manager.collection is not None, "the fixture never got as far as opening"
    (manager.cfg.chroma_path / "chroma.sqlite3").write_bytes(b"changed")
    manager._ensure_ready()

    assert len(built) == 1, f"the embedding function was rebuilt on reopen: {built}"


def test_the_configured_execution_caps_reach_the_embedding_function(tmp_path, monkeypatch):
    """The config fields exist but stay inert until they are passed here, and an
    uncapped encode is what exhausted a 16 GB card mid-reindex. Asserted against
    the values this Config carries, not against the shipped defaults."""
    built = []

    def fake_builder(model, max_seq_length=None, batch_size=None, device=None):
        built.append({"model": model, "max_seq_length": max_seq_length,
                      "batch_size": batch_size})
        return object()

    monkeypatch.setattr("delegation_core.vault.make_bge_embedding_function", fake_builder)

    class FakeCollection:
        def count(self):
            return 0

    class FakeClient:
        def get_or_create_collection(self, **kwargs):
            return FakeCollection()

    import sys
    import types

    fake_chromadb = types.ModuleType("chromadb")
    fake_chromadb.Settings = lambda **kw: None
    fake_chromadb.PersistentClient = lambda **kw: FakeClient()
    monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)

    cfg = Config(vault_path=str(tmp_path / "vault"))
    cfg.embed_max_seq_length = 777
    cfg.embed_batch_size = 3
    VaultManager(cfg)._init()

    assert built == [{"model": cfg.bge_model, "max_seq_length": 777, "batch_size": 3}]
