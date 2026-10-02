"""The lock that keeps a reopen of the index from running under a query.

Kept out of vault.py, which the notes split holds under 1800 lines, and free of
any import from it.
"""

import functools
import logging
import threading
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class IndexUseLock:
    """Shared/exclusive lock between index users and the reopen.

    Every call that touches `collection` holds it shared; the reopen holds it
    exclusive. Without it the reopen dropped the client while other threads were
    mid-query on it, and on 2026-09-26 the Mac daemon deadlocked seconds after
    one: a thread stuck inside chromadb holding the GIL, 234 of 285 threads
    belonging to orphaned chromadb runtimes.

    Readers are preferred, so a thread already holding it shared can take it
    shared again even while a reopen waits. The reopen cannot run from a thread
    that holds it shared (it would wait on itself); `held_here()` lets it skip
    and try again on the next call instead.

    The thread holding it exclusive may take it shared: the reopen runs
    `_init`, and anything `_init` calls that reads the index would otherwise
    wait on the reopen that called it. That happened on master ad8049f, where
    `_init` began logging `get_stats()`: the first reopen hung the daemon.
    """

    def __init__(self):
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer = False
        self._writer_ident = None
        self._local = threading.local()

    def _writing_here(self) -> bool:
        return self._writer_ident == threading.get_ident()

    def held_here(self) -> bool:
        return self._writing_here() or getattr(self._local, "depth", 0) > 0

    @contextmanager
    def shared(self):
        if self._writing_here():
            # Already exclusive on this thread: nothing else can be reading or
            # reopening, so there is nothing to wait for.
            yield
            return
        with self._cond:
            while self._writer:
                self._cond.wait()
            self._readers += 1
        self._local.depth = getattr(self._local, "depth", 0) + 1
        try:
            yield
        finally:
            self._local.depth -= 1
            with self._cond:
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()

    @contextmanager
    def exclusive(self):
        with self._cond:
            while self._writer or self._readers:
                self._cond.wait()
            self._writer = True
            self._writer_ident = threading.get_ident()
        try:
            yield
        finally:
            with self._cond:
                self._writer = False
                self._writer_ident = None
                self._cond.notify_all()


def uses_index(fn):
    """Run a VaultManager method with the index held shared.

    `_ensure_ready()` runs first, outside the lock, because it is where a reopen
    happens and a reopen needs the lock exclusive. The method's own call to it
    then finds the index ready and does not reopen underneath itself.
    """
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        self._ensure_ready()
        with index_lock_of(self).shared():
            return fn(self, *args, **kwargs)
    return wrapper


def reads_index(fn):
    """Like `uses_index`, for helpers that never initialised the index themselves."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with index_lock_of(self).shared():
            return fn(self, *args, **kwargs)
    return wrapper


_creating = threading.Lock()


def index_lock_of(manager) -> IndexUseLock:
    """The manager's lock, created on first use.

    Lazy rather than set in __init__, so a manager built without it (tests use
    VaultManager.__new__) still gets one, and never two.
    """
    lock = manager.__dict__.get("_index_use_lock")
    if lock is None:
        with _creating:
            lock = manager.__dict__.setdefault("_index_use_lock", IndexUseLock())
    return lock


def close_chroma_client(client) -> None:
    """Stop the current chromadb client so a reopen starts from a fresh System.

    chromadb caches one System per path for the life of the process, so a new
    PersistentClient alone shares the stale segment state and keeps failing
    filtered queries with "Error finding id". The old fix emptied that cache
    with clear_system_cache(), which only forgets the Systems: it never stops
    them, so every reopen left a chromadb runtime and its threads running
    (234 of 285 threads on the Mac daemon that deadlocked on 2026-09-26).
    close() drops the reference and stops the System once nothing else holds it.
    """
    if client is None:
        return
    try:
        from chromadb.api.client import SharedSystemClient
    except Exception as e:  # pragma: no cover - depends on chromadb internals
        logger.warning("Could not import chromadb client internals: %s", e)
        return
    close = getattr(client, "close", None)
    if close is None:
        # chromadb without Client.close() (older than the 1.x line): the old
        # behaviour, which leaks the System but still yields a fresh one.
        SharedSystemClient.clear_system_cache()
        return
    try:
        close()
    except Exception as e:
        logger.warning("Closing the old chromadb client failed: %s", e)
    # Something else in this process still holds the same System (another
    # PersistentClient on this path). Stopping it would break that holder, so
    # it is only forgotten, as before, and the reopen still gets a fresh one.
    ident = getattr(client, "_identifier", None)
    cache = getattr(SharedSystemClient, "_identifier_to_system", {})
    if ident is not None and ident in cache:
        logger.warning("chromadb System still shared after close; dropping it from the cache")
        cache.pop(ident, None)
        getattr(SharedSystemClient, "_identifier_to_refcount", {}).pop(ident, None)
