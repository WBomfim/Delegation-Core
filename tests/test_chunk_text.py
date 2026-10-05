"""embeddings.chunk_text — the splitter both ingest and (since v0.12) the vault use.

Two defects were fixed here at once, and both were reachable from a config file
rather than from code:

  * `overlap >= max_chars` makes the stride `max_chars - overlap` zero or
    negative, and the loop then appends forever. chunk_text("x" * 10000, 100, 100)
    grew until the OOM killer took the process (exit 137). Both sizes come from
    user-editable config — ingest_chunk_size/ingest_chunk_overlap and
    vault_chunk_size/vault_chunk_overlap — so a typo could take the box down.
  * The loop took one stride too many at the end, emitting a tail that lay wholly
    inside its predecessor's overlap: chunk_text("x" * 7601) returned lengths
    [4000, 3801, 1]. That 1-character duplicate became its own ChromaDB row with
    its own embedding, competing in search against content already covered.

Sizes are passed explicitly throughout. The shipped defaults are a tuning
decision that may move; what must not move is the behaviour asserted here.
"""

import signal

import pytest

from delegation_core.embeddings import chunk_text


def covers_exactly(text, chunks, max_chars, overlap):
    """The full-coverage invariant: consecutive chunks advance by one stride and
    the last one runs to the end of the text, so the strides plus the final chunk
    account for every character exactly once."""
    stride = max_chars - overlap
    return (len(chunks) - 1) * stride + len(chunks[-1]) == len(text)


# ── splitting ────────────────────────────────────────────────────────────────

def test_a_text_shorter_than_the_window_is_returned_whole():
    assert chunk_text("a short note", max_chars=4000, overlap=200) == ["a short note"]


def test_a_text_exactly_the_window_is_not_split():
    text = "x" * 4000
    assert chunk_text(text, max_chars=4000, overlap=200) == [text]


def test_one_character_past_the_window_splits_in_two():
    chunks = chunk_text("x" * 4001, max_chars=4000, overlap=200)

    assert [len(c) for c in chunks] == [4000, 201]
    assert covers_exactly("x" * 4001, chunks, 4000, 200)


def test_the_chunks_reassemble_into_the_original_text():
    """Not merely "nothing was dropped" — the overlap makes each chunk start
    exactly one stride after the last, so stitching by stride rebuilds the input."""
    text = "".join(chr(97 + i % 26) for i in range(23_500))
    max_chars, overlap = 4000, 200

    chunks = chunk_text(text, max_chars=max_chars, overlap=overlap)

    stride = max_chars - overlap
    assert "".join(c[:stride] for c in chunks[:-1]) + chunks[-1] == text
    assert covers_exactly(text, chunks, max_chars, overlap)


def test_consecutive_chunks_share_exactly_the_overlap():
    text = "".join(chr(97 + i % 26) for i in range(20_000))

    chunks = chunk_text(text, max_chars=4000, overlap=200)

    for earlier, later in zip(chunks, chunks[1:]):
        assert earlier[-200:] == later[:200]


@pytest.mark.parametrize("length", [4001, 5000, 7600, 7601, 8000, 12_000, 40_000])
def test_full_coverage_holds_at_every_length(length):
    text = "x" * length
    chunks = chunk_text(text, max_chars=4000, overlap=200)

    assert covers_exactly(text, chunks, 4000, 200)


def test_no_redundant_trailing_chunk():
    """chunk_text("x" * 7601) was [4000, 3801, 1]: a third chunk lying wholly
    inside the second one's overlap, which cost a whole embedding and a whole
    ChromaDB row to say nothing new."""
    assert [len(c) for c in chunk_text("x" * 7601, max_chars=4000, overlap=200)] == [4000, 3801]


def test_no_chunk_is_contained_in_its_predecessor():
    """The general form of the above — a chunk that adds no character its
    predecessor did not already carry is pure waste, at any length."""
    for length in range(7590, 7650):
        chunks = chunk_text("x" * length, max_chars=4000, overlap=200)
        stride = 4000 - 200
        assert len(chunks[-1]) > 200 or len(chunks) == 1, (
            f"length={length} produced a tail of {len(chunks[-1])} chars, "
            f"which the previous chunk's {200}-char overlap already covered")
        assert covers_exactly("x" * length, chunks, 4000, 200)
        assert (len(chunks) - 1) * stride < length


# ── the overlap clamp ────────────────────────────────────────────────────────

def test_an_overlap_of_exactly_half_the_window_is_allowed(caplog):
    """The clamp's boundary, and the one value that must NOT be adjusted —
    max_chars // 2 is the largest legal overlap, not the first illegal one."""
    with caplog.at_level("WARNING", logger="embeddings"):
        chunks = chunk_text("x" * 1000, max_chars=100, overlap=50)

    assert "clamping" not in caplog.text
    assert covers_exactly("x" * 1000, chunks, 100, 50)
    assert len(chunks) == 19


def test_an_overlap_one_past_half_is_clamped_back_to_half():
    """Half rather than max_chars - 1: a stride of one character is barely less
    ruinous than a stride of zero, just slower to notice."""
    over = chunk_text("x" * 1000, max_chars=100, overlap=51)
    at_half = chunk_text("x" * 1000, max_chars=100, overlap=50)

    assert over == at_half


def test_the_clamp_uses_floor_division_on_an_odd_window():
    assert chunk_text("x" * 1000, max_chars=101, overlap=50) == \
        chunk_text("x" * 1000, max_chars=101, overlap=99)


def test_an_overlap_equal_to_the_window_is_clamped_not_looped():
    """REGRESSION: stride 0. This call used to append forever and die to the OOM
    killer. Bounded by SIGALRM so a reintroduced runaway fails this test instead
    of hanging (and then eating) the whole suite."""
    with _deadline(2.0):
        chunks = chunk_text("x" * 1000, max_chars=100, overlap=100)

    assert len(chunks) == 19
    assert covers_exactly("x" * 1000, chunks, 100, 50)


def test_an_overlap_larger_than_the_window_is_clamped_not_looped():
    """REGRESSION: negative stride — the loop walks backwards and never ends."""
    with _deadline(2.0):
        chunks = chunk_text("x" * 1000, max_chars=100, overlap=100_000)

    assert len(chunks) == 19
    assert covers_exactly("x" * 1000, chunks, 100, 50)


def test_a_negative_overlap_is_clamped_up_to_zero():
    """Clamping down to max_chars // 2 would be wrong here — a negative overlap
    means a stride *larger* than the window, which would skip text entirely."""
    text = "x" * 1000

    chunks = chunk_text(text, max_chars=100, overlap=-10)

    assert [len(c) for c in chunks] == [100] * 10
    assert "".join(chunks) == text


def test_the_clamp_is_reported_rather_than_applied_silently(caplog):
    with caplog.at_level("WARNING", logger="embeddings"):
        chunk_text("x" * 1000, max_chars=100, overlap=100)

    assert "clamping to 50" in caplog.text


# ── degenerate windows ───────────────────────────────────────────────────────

def test_empty_text_is_one_empty_chunk():
    """One row of nothing rather than zero rows: callers zip ids against chunks,
    and an empty list would quietly index nothing at all."""
    assert chunk_text("", max_chars=4000, overlap=200) == [""]


def test_a_max_chars_of_zero_returns_the_text_unsplit(caplog):
    """Every stride would be non-positive; refusing to split is the only sane
    answer, and it must be reported rather than looked like a short text."""
    with caplog.at_level("WARNING", logger="embeddings"):
        assert chunk_text("x" * 1000, max_chars=0, overlap=0) == ["x" * 1000]

    assert "max_chars=0" in caplog.text


def test_a_negative_max_chars_returns_the_text_unsplit():
    assert chunk_text("x" * 1000, max_chars=-5, overlap=200) == ["x" * 1000]


def test_a_window_of_one_still_terminates():
    """max_chars // 2 == 0, so the overlap clamps to 0 and the stride to 1 — the
    smallest window that can still make progress."""
    with _deadline(2.0):
        chunks = chunk_text("x" * 500, max_chars=1, overlap=1)

    assert len(chunks) == 500


# ── hang guard ───────────────────────────────────────────────────────────────

class _RunawayLoop(Exception):
    pass


class _deadline:
    """Wall-clock bound on a block, via SIGALRM.

    The failures under test are unbounded loops that allocate on every
    iteration, so a plain assertion cannot catch them — the test process is gone
    before it runs. pytest executes tests on the main thread, where SIGALRM is
    deliverable, and the handler raises out of the loop after a fraction of a
    second's worth of growth rather than gigabytes of it.
    """

    def __init__(self, seconds):
        self.seconds = seconds

    def __enter__(self):
        if not hasattr(signal, "SIGALRM"):
            # Windows: sem SIGALRM nao ha como interromper o laco na thread
            # principal. A logica e a mesma em toda plataforma; Linux e macOS
            # cobrem.
            pytest.skip("precisa de SIGALRM")

        def fire(signum, frame):
            raise _RunawayLoop(f"chunk_text did not finish within {self.seconds}s")

        self._previous = signal.signal(signal.SIGALRM, fire)
        signal.setitimer(signal.ITIMER_REAL, self.seconds)
        return self

    def __exit__(self, exc_type, exc, tb):
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, self._previous)
        return False
