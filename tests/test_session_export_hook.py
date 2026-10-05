"""session_export.py: the SessionEnd hook writes to the vault's configured Sessions folder.

The hook hardcoded ``vault / "sessions"``. On this vault — and any vault set up
with Capitalized folder names — that created a second, lowercase ``sessions/``
directory beside the real ``Sessions/``. Indexing, health accounting and search
all iterate ``cfg.vault_folders``, so every transcript written there was
invisible: absent from ChromaDB, unsearchable, uncounted. 29 transcripts spanning
2026-06-10 to 2026-07-30 had accumulated there before it was noticed.

The hook is stdlib-only by design (runs under system python3, no venv), so it
cannot import config.resolve_folder — hence its own resolver, pinned here.

Loaded by path rather than imported: hooks/ is not a package on sys.path.
"""

import importlib.util
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "src" / "delegation_core" / "hooks" / "session_export.py"


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("session_export", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CAPS = ["Projects", "Decisions", "Fixes", "Sessions", "Procedures",
        "Reference", "Tools", "Scratch", "Infrastructure"]


def test_resolves_capitalized_sessions_folder(hook, tmp_path):
    resolved = hook._resolve_sessions_dir(tmp_path, {"vault_folders": CAPS})
    assert resolved == tmp_path / "Sessions"


def test_resolves_lowercase_sessions_folder(hook, tmp_path):
    resolved = hook._resolve_sessions_dir(tmp_path, {"vault_folders": ["decisions", "sessions"]})
    assert resolved == tmp_path / "sessions"


def test_resolves_odd_casing_and_whitespace(hook, tmp_path):
    resolved = hook._resolve_sessions_dir(tmp_path, {"vault_folders": ["  SESSIONS  "]})
    assert resolved == tmp_path / "SESSIONS"


def test_defaults_to_capitalized_when_config_has_no_sessions_folder(hook, tmp_path):
    """Capitalized is the shipped wizard default, so an unconfigured vault should
    not be the one that gets a stray lowercase directory."""
    assert hook._resolve_sessions_dir(tmp_path, {"vault_folders": ["Notes"]}) == tmp_path / "Sessions"
    assert hook._resolve_sessions_dir(tmp_path, {}) == tmp_path / "Sessions"


def test_ignores_non_string_entries_in_vault_folders(hook, tmp_path):
    """A hand-edited config.json must not crash the hook on session close."""
    resolved = hook._resolve_sessions_dir(tmp_path, {"vault_folders": [None, 3, "Sessions"]})
    assert resolved == tmp_path / "Sessions"
