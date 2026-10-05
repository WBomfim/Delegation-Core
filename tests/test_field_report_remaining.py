"""The five Field Report fixes that were applied downstream but never merged.

All five were live on the reporting deployment and none were in the tree — the
eight harder defects landed while these, already written, did not. Pinned here
so the same thing cannot happen twice.
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

from delegation_core.config import Config
from delegation_core.service import launchd_plist_text

HOOKS = Path(__file__).resolve().parent.parent / "src" / "delegation_core" / "hooks"
sys.path.insert(0, str(HOOKS))


# ── #3: the default search scope must follow the vault, not one machine ──────

def _scope_for(total, generated, configured=""):
    import delegation_core.server as server

    class _V:
        cfg = Config(vault_path="/tmp/v", default_search_scope=configured)

        def get_health_summary(self):
            return {"total_notes": total, "generated_notes": generated}

    old = server._vault
    server._vault = _V()
    try:
        return server._default_scope()
    finally:
        server._vault = old


def test_a_generated_heavy_vault_defaults_to_notes():
    """Machine output in the majority: an unscoped search would be answered by
    it, and the user's own writing loses."""
    assert _scope_for(total=3662, generated=3432) == "notes"


def test_an_ingest_heavy_vault_defaults_to_all():
    """The mirror case the fixed default got wrong: 6,637 ingested files were
    the authoritative corpus and scope='notes' hid every one of them."""
    assert _scope_for(total=640, generated=0) == "all"


def test_an_explicit_config_setting_wins_over_the_heuristic():
    assert _scope_for(total=640, generated=0, configured="notes") == "notes"


def test_an_unreadable_health_pass_narrows_rather_than_widens():
    import delegation_core.server as server

    class _Broken:
        cfg = Config(vault_path="/tmp/v")

        def get_health_summary(self):
            raise RuntimeError("health pass exploded")

    old = server._vault
    server._vault = _Broken()
    try:
        assert server._default_scope() == "notes"
    finally:
        server._vault = old


# ── #5: status must ask for the collection this install actually uses ────────

def test_the_collection_name_follows_the_embedding_model():
    """cli status looked up the literal "vault_bge" while every other caller
    derived it, so any non-bge-base install reported a healthy index as
    uninitialised and advised an hours-long rebuild."""
    assert Config(vault_path="/tmp/v", bge_model="BAAI/bge-m3").collection_name == "vault_bge_m3"
    assert Config(vault_path="/tmp/v", bge_model="BAAI/bge-base-en-v1.5").collection_name == "vault_bge"


def test_cli_status_does_not_hardcode_a_collection():
    source = (Path(__file__).resolve().parent.parent
              / "src" / "delegation_core" / "cli.py").read_text(encoding="utf-8")
    assert 'get_collection("vault_bge")' not in source


# ── #11: the transcript filename must be stable across exports ───────────────

def test_the_transcript_name_carries_no_date():
    """A date in the name changes when a session is resumed on another day, so
    the same session produced a second partial note: 111 transcripts for 47
    real sessions on the reporting install."""
    source = (HOOKS / "session_export.py").read_text(encoding="utf-8")
    assert 'f"transcript-{short_id}.md"' in source
    assert 'f"{date_str}-transcript-' not in source


def test_the_transcript_is_dated_by_its_first_message():
    from session_export import _session_date
    msgs = [{"role": "user", "ts": "2026-07-30T21:15:00", "text": "hi"},
            {"role": "assistant", "ts": "2026-07-31T02:00:00", "text": "yo"}]
    assert _session_date(msgs) == "2026-07-30"


def test_an_undated_transcript_falls_back_to_today():
    from session_export import _session_date
    assert _session_date([{"role": "user", "ts": "", "text": "hi"}]) == \
        datetime.now().strftime("%Y-%m-%d")


def test_re_exporting_a_session_replaces_it_rather_than_skipping():
    source = (HOOKS / "session_export.py").read_text(encoding="utf-8")
    assert "already exists — skipping" not in source, (
        "the later export is the more complete one; it must replace"
    )


# ── #12: the launchd agent must not inherit maxfiles 256 ─────────────────────

def test_the_plist_raises_the_descriptor_limit():
    plist = launchd_plist_text()
    assert "SoftResourceLimits" in plist
    assert "NumberOfFiles" in plist
    assert "16384" in plist


# ── #13: compress must honour synthesis_lang ─────────────────────────────────

def test_compress_injects_the_configured_language():
    source = (Path(__file__).resolve().parent.parent
              / "src" / "delegation_core" / "server.py").read_text(encoding="utf-8")
    body = source[source.index("async def _compress"):][:1200]
    assert "synthesis_lang" in body, (
        "the prompt was hardcoded English while organizer honoured the setting, "
        "so a batch produced a bilingual vault by accident"
    )


# ── the daemon must be allowed to finish writing before it is killed ─────────

def test_the_systemd_unit_allows_a_long_shutdown():
    """A restart issued during a relink hit systemd's 10s stop ceiling, SIGKILLed
    the daemon with ChromaDB mid-write, and the next two starts died with SIGSEGV
    inside chromadb_rust_bindings. Only the third came up."""
    from delegation_core.service import systemd_unit_text
    unit = systemd_unit_text()
    assert "TimeoutStopSec=" in unit
    value = int(unit.split("TimeoutStopSec=")[1].split()[0])
    assert value >= 300, "shorter than a real reindex is no protection at all"


def test_the_launchd_plist_allows_a_long_shutdown():
    from delegation_core.service import launchd_plist_text
    plist = launchd_plist_text()
    assert "ExitTimeOut" in plist, "launchd's default is 20s between SIGTERM and SIGKILL"
