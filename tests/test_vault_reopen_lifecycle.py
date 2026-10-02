"""The reopen against a real chromadb: it must stop the old System, and it must
not pull the client out from under a query in flight.

On 2026-09-26 the Mac daemon deadlocked seconds after a reopen, with six tool
calls arriving at once. The process held 285 threads, 234 of them chromadb
runtimes, and one thread sat inside chromadb holding the GIL.

Measured on chromadb 1.5.9 before this fix: every reopen through
clear_system_cache() left 27 more OS threads running, because emptying the
cache forgets a System without stopping it. Client.close() leaves none.

test_vault_reload.py covers when a reopen happens, with a fake chromadb; these
cover what a reopen does, with the real one.
"""

import os
import threading
import time

import pytest

from delegation_core import gpu
from delegation_core.config import Config
from delegation_core.index_lock import index_lock_of
from delegation_core.vault import VaultManager


class _Embedder:
    """Deterministic bag-of-words vectors: no model, no GPU.

    A query can be held inside chromadb: clear `gate` and the next embed_query
    sets `entered` and waits, the way a slow embedding holds a real search.
    """

    def __init__(self):
        self.gate = threading.Event()
        self.gate.set()
        self.entered = threading.Event()

    def __call__(self, input):
        out = []
        for text in input:
            v = [0.0] * 16
            for w in str(text).lower().split():
                v[hash(w) % 16] += 1.0
            n = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / n for x in v])
        return out

    def embed_documents(self, input):
        return self(input)

    def embed_query(self, input):
        if not self.gate.is_set():
            self.entered.set()
            self.gate.wait(5)
        if isinstance(input, str):
            return self([input])[0]
        return self(list(input))

    def name(self):
        return "probe"


def _os_threads() -> int:
    return len(os.listdir("/proc/self/task"))


@pytest.fixture
def vm(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu, "take", lambda *a, **k: None)
    cfg = Config(vault_path=str(tmp_path / "vault"), vault_folders=["Notes"],
                 search_threshold=0.0)
    (tmp_path / "vault" / "Notes").mkdir(parents=True)
    manager = VaultManager(cfg)
    manager.ef = _Embedder()      # set before _init, so BGE is never built
    manager._init()
    assert manager._initialized, "the fixture never opened the real index"
    yield manager
    manager._close_client()


def _foreign_write(vm):
    """Make the fingerprint disagree, as a write by another process would."""
    vm._disk_state = ("written", "elsewhere")


@pytest.mark.skipif(not os.path.isdir("/proc/self/task"), reason="counts threads via /proc")
def test_reopens_do_not_accumulate_chromadb_threads(vm):
    vm.collection.count()
    _foreign_write(vm)
    vm._ensure_ready()           # first reopen may start lazily-created threads
    vm.collection.count()
    before = _os_threads()

    for _ in range(5):
        _foreign_write(vm)
        vm._ensure_ready()
        vm.collection.count()

    grew = _os_threads() - before
    assert grew < 10, (f"{grew} threads left behind by 5 reopens; "
                       "clear_system_cache() alone leaked 27 per reopen")


def test_the_previous_client_is_closed_and_forgotten(vm):
    from chromadb.api.client import SharedSystemClient

    old = vm._client
    _foreign_write(vm)
    vm._ensure_ready()

    assert vm._client is not old
    assert old._closed
    assert len(SharedSystemClient._identifier_to_system) == 1


def test_data_is_still_searchable_after_a_reopen(vm):
    vm.index_note("the quarterly vendor decision", {
        "title": "vendor", "path": "Notes/vendor.md", "folder": "Notes"})
    _foreign_write(vm)

    hits = vm.search("quarterly vendor decision", limit=3)

    assert hits and hits[0].get("path") == "Notes/vendor.md", hits


def test_a_reopen_waits_for_a_query_in_flight(vm):
    old = vm._client
    in_query = threading.Event()
    finish_query = threading.Event()

    def reader():
        with index_lock_of(vm).shared():
            in_query.set()
            finish_query.wait(5)
            # Still the client this query started on, and still open.
            assert vm._client is old and not old._closed
            vm.collection.count()

    t_reader = threading.Thread(target=reader)
    t_reader.start()
    assert in_query.wait(5)

    _foreign_write(vm)
    t_reopen = threading.Thread(target=vm._ensure_ready)
    t_reopen.start()
    time.sleep(0.3)
    assert t_reopen.is_alive(), "the reopen ran while a query was using the client"

    finish_query.set()
    t_reader.join(5)
    t_reopen.join(5)
    assert not t_reopen.is_alive()
    assert vm._client is not old


def test_a_reopen_waits_for_a_search_in_flight(vm):
    """The same, through search() itself rather than the lock by hand: the
    decorator has to be what holds the client in place."""
    vm.index_note("vendor decision", {
        "title": "vendor", "path": "Notes/vendor.md", "folder": "Notes"})
    old = vm._client
    vm.ef.gate.clear()
    results = []
    t_search = threading.Thread(target=lambda: results.append(vm.search("vendor", limit=1)))
    t_search.start()
    assert vm.ef.entered.wait(5), "the search never reached the embedder"

    _foreign_write(vm)
    t_reopen = threading.Thread(target=vm._ensure_ready)
    t_reopen.start()
    time.sleep(0.3)
    reopened_mid_search = not t_reopen.is_alive()

    vm.ef.gate.set()
    t_search.join(5)
    t_reopen.join(5)
    assert not reopened_mid_search, "the reopen closed the client under a running search"
    assert results and results[0] and "error" not in results[0][0], results
    assert vm._client is not old


def test_a_reopen_requested_inside_a_query_is_deferred_not_deadlocked(vm):
    old = vm._client
    done = threading.Event()

    def nested():
        with index_lock_of(vm).shared():
            _foreign_write(vm)
            vm._ensure_ready()       # would wait on itself if it tried to reopen
        done.set()

    t = threading.Thread(target=nested, daemon=True)
    t.start()
    assert done.wait(5), "a reopen from inside a query waited on its own lock"
    assert vm._client is old, "reopened underneath the query that asked"

    vm._ensure_ready()
    assert vm._client is not old, "the pending reopen was lost instead of deferred"


def test_the_reopening_thread_can_read_the_index_it_holds():
    """The reopen holds the lock exclusive while it runs _init, and _init reads
    the index (on master ad8049f it logs get_stats()). A shared request from
    that same thread must enter, not wait on its own exclusive hold."""
    from delegation_core.index_lock import IndexUseLock

    lock = IndexUseLock()
    done = threading.Event()

    def reopen():
        with lock.exclusive():
            assert lock.held_here(), "a nested reopen on this thread would wait on itself"
            with lock.shared():
                pass
        done.set()

    threading.Thread(target=reopen, daemon=True).start()
    assert done.wait(5), "the exclusive holder deadlocked on its own shared request"


def test_a_reopen_finishes_when_init_reads_the_index(vm):
    """The real reopen, end to end, bounded: it hung forever on ad8049f."""
    old = vm._client
    _foreign_write(vm)
    t = threading.Thread(target=vm._ensure_ready, daemon=True)
    t.start()
    t.join(10)

    assert not t.is_alive(), "the reopen deadlocked inside _init"
    assert vm._client is not old
    assert vm.get_stats()["indexed_rows"] >= 0
