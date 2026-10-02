"""`status` and `embed-model` count the index without opening it under the daemon.

Measured on chromadb 1.5.9: opening a PersistentClient, even only to count,
changes chroma.sqlite3's mtime. The running daemon reads that as another
process's write and reopens its index, so every `delegation-core status` was a
reopen, and each reopen used to leave 27 chromadb threads behind.
"""

import pytest

from delegation_core import cli, daemon
from delegation_core.config import Config


@pytest.fixture
def cfg(tmp_path):
    return Config(vault_path=str(tmp_path / "vault"))


@pytest.fixture
def no_local_open(monkeypatch):
    import chromadb

    def refuse(*a, **k):
        raise AssertionError("opened the index beside a running daemon")

    monkeypatch.setattr(chromadb, "PersistentClient", refuse)


def test_a_running_daemon_is_asked(cfg, monkeypatch, no_local_open):
    monkeypatch.setattr(daemon, "is_listening", lambda cfg, **k: True)
    monkeypatch.setattr(daemon, "call_tool",
                        lambda cfg, tool, *a, **k: {"indexed_rows": 58518})

    counts, source = cli._index_row_counts(cfg)

    assert source == "daemon"
    assert counts == {cfg.collection_name: 58518}


def test_a_daemon_that_does_not_answer_is_reported_not_bypassed(cfg, monkeypatch,
                                                                no_local_open):
    """The 2026-09-26 failure: socket open, every call timing out."""
    def hang(cfg, tool, *a, **k):
        raise TimeoutError("initialize timed out")

    monkeypatch.setattr(daemon, "is_listening", lambda cfg, **k: True)
    monkeypatch.setattr(daemon, "call_tool", hang)

    counts, source = cli._index_row_counts(cfg)

    assert counts == {}
    assert source.startswith("daemon-unresponsive")


def test_without_a_daemon_it_counts_locally_and_closes(cfg, monkeypatch):
    import chromadb
    from chromadb.api.client import SharedSystemClient

    monkeypatch.setattr(daemon, "is_listening", lambda cfg, **k: False)
    cfg.chroma_path.mkdir(parents=True)
    seed = chromadb.PersistentClient(path=str(cfg.chroma_path))
    seed.get_or_create_collection("probe_rows").add(
        ids=["a", "b"], documents=["x", "y"], embeddings=[[0.1, 0.2], [0.2, 0.1]])
    seed.close()

    counts, source = cli._index_row_counts(cfg)

    assert source == "local"
    assert counts == {"probe_rows": 2}
    assert SharedSystemClient._identifier_to_system == {}, "left the client open"
