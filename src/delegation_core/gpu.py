"""Mutual exclusion for the one GPU that BGE-m3 and llama.cpp both want.

Two residents, one card. On the 16 GB board this runs on, the 12B model's
weights alone are 9.7 GB and BGE-m3 holds 4.8 GB, so whichever loads second
dies at cudaMalloc. Until now nothing arbitrated that: `make_bge_embedding_function`
knew about the contention and answered it by falling back to CPU in silence,
which trades a hard failure for a search that is quietly ten times slower, and
`engine._start()` did not know about it at all, so llama.cpp simply failed to
load whenever a search had touched the vault first.

This module is the arbiter. Exactly one resident holds the card at a time, and
whoever needs it evicts the other first. Both sides reload lazily, so eviction
costs a cold start on the next call and nothing else.

The two owners register themselves rather than being imported here, because
vault imports embeddings and engine imports config, and reaching back into
either from this module would close an import cycle. Registration is by
weakref: an owner that goes out of scope stops being asked to release.
"""

from __future__ import annotations

import gc
import logging
import subprocess
import threading
import weakref
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# Reentrant: release_embeddings() is reachable from inside a hold, and a
# non-reentrant lock would deadlock the thread that already owns the card.
_lock = threading.RLock()

_embeddings_owners: "weakref.WeakSet" = weakref.WeakSet()
_llama_owners: "weakref.WeakSet" = weakref.WeakSet()

# Who is believed to hold the card. Advisory only: the truth is what is
# resident on the device, and vram_free_mib() is how that gets checked.
_holder: str | None = None


def register_embeddings_owner(owner) -> None:
    """Register an object holding a BGE embedding function (a VaultIndex)."""
    with _lock:
        _embeddings_owners.add(owner)


def register_llama_owner(owner) -> None:
    """Register an object holding a llama.cpp process (a DelegationEngine)."""
    with _lock:
        _llama_owners.add(owner)


def holder() -> str | None:
    """Which resident the arbiter last handed the card to, or None."""
    return _holder


def vram_free_mib() -> int | None:
    """Free VRAM in MiB, or None when there is no NVIDIA card to ask.

    Deliberately shells out to nvidia-smi rather than importing torch: this is
    called on the path that is about to *free* torch's memory, and importing
    torch to measure would itself allocate a CUDA context.
    """
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip().splitlines()
        return int(out[0].strip()) if out else None
    except Exception:
        return None


def embeddings_resident() -> bool:
    """True when a SentenceTransformer is cached on a CUDA device.

    Reads STEF's class-level cache directly. That dict is the single place a
    loaded BGE lives: every embedding function over one model name shares the
    instance in it, which is exactly why dropping one caller's reference is not
    enough to free the weights.
    """
    try:
        from chromadb.utils.embedding_functions import (
            SentenceTransformerEmbeddingFunction as _STEF,
        )
    except Exception:
        return False
    for model in list(getattr(_STEF, "models", {}).values()):
        device = getattr(model, "device", None)
        if device is not None and "cuda" in str(device):
            return True
    return False


def release_embeddings() -> int:
    """Evict BGE from the GPU. Returns MiB freed, as measured on the device.

    Three references have to go, and missing any one of them leaves the weights
    resident while looking released:

      1. STEF's class-level `models` dict, which is the owning reference
      2. each registered owner's `ef` and `collection` (a chroma collection
         holds the embedding function, so clearing `ef` alone is not enough)
      3. whatever gc has not yet swept, hence the explicit collect

    Only then does empty_cache() have anything to hand back to the driver.
    """
    with _lock:
        global _holder
        if not embeddings_resident():
            return 0

        before = vram_free_mib()

        for owner in list(_embeddings_owners):
            try:
                owner.ef = None
                owner.collection = None
                # Leave _initialized False so the next call reopens rather than
                # querying a collection that is no longer there.
                if hasattr(owner, "_initialized"):
                    owner._initialized = False
            except Exception as e:
                logger.warning("could not clear embedding owner %r: %s", owner, e)

        try:
            from chromadb.utils.embedding_functions import (
                SentenceTransformerEmbeddingFunction as _STEF,
            )
            getattr(_STEF, "models", {}).clear()
        except Exception as e:
            logger.warning("could not clear the SentenceTransformer cache: %s", e)

        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
        except Exception as e:
            logger.warning("torch.cuda.empty_cache() failed: %s", e)

        after = vram_free_mib()
        freed = (after - before) if (before is not None and after is not None) else 0
        if _holder == "embeddings":
            _holder = None
        logger.info("released BGE from the GPU, %d MiB freed", freed)
        return max(freed, 0)


def release_llama() -> int:
    """Stop every registered llama.cpp this process started. Returns MiB freed.

    `_shutdown()` only terminates a process the engine itself spawned, so a
    llama-server someone is running by hand is left alone. That is the right
    boundary and it is kept here: the arbiter does not kill what it did not
    start, it only reports that the card is still occupied.
    """
    with _lock:
        global _holder
        before = vram_free_mib()
        stopped = False
        for owner in list(_llama_owners):
            try:
                if owner._proc is not None:
                    owner._shutdown()
                    stopped = True
            except Exception as e:
                logger.warning("could not stop llama.cpp on %r: %s", owner, e)

        if not stopped:
            return 0

        after = vram_free_mib()
        freed = (after - before) if (before is not None and after is not None) else 0
        if _holder == "llama":
            _holder = None
        logger.info("stopped llama.cpp, %d MiB freed", freed)
        return max(freed, 0)


def take(who: str) -> int:
    """Take the card for `who` ("llama" or "embeddings"), evicting the other.

    Returns MiB reclaimed from the evicted resident, 0 when it was not loaded.

    There is no matching release, by design. Both residents are meant to stay
    loaded and serve many calls, so holding the card is the steady state and
    handing it over is the event. What this guarantees is only that at the
    moment one loads, the other is gone.
    """
    if who not in ("llama", "embeddings"):
        raise ValueError(f"unknown GPU resident: {who!r}")
    with _lock:
        global _holder
        if _holder == who:
            return 0
        # Evict the OTHER resident, not the one asking. Getting this backwards
        # is quiet rather than loud: llama.cpp's layer fitting simply loads
        # fewer layers so it squeezes in beside the BGE that was never evicted,
        # and the only symptom is a slower model on a card that looks full.
        freed = release_embeddings() if who == "llama" else release_llama()
        _holder = who
        if freed:
            logger.info("GPU handed to %s, %d MiB reclaimed", who, freed)
        return freed


@contextmanager
def held_by(who: str):
    """`take` as a context manager, for call sites that read better that way.

    Exiting the block does not give the card back, for the reason in `take`.
    """
    take(who)
    yield

