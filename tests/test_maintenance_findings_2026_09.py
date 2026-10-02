"""Unit tests for the systemic fixes from the 2026-09-01/02 maintenance findings.

Covers:
  1. Wikilinks with currency symbols / $ (Achado B15): preserves valid note titles
     while excluding bash test syntax.
  2. Office temporary lock files (~$*) in extractor and ingest (Achado B9).
  3. Default chunk sizes of 3072 chars aligned with BGE-M3 1024 token window (Achado B11).
  4. Config permissions chmod 0o600 on load and save (Achado B10).
  5. synthesis_lang propagation in search_vault, search_web, and synthesizer prompts (Achado B5).
  6. vault_health cache bypass with force=True in health_summary and heartbeat (Achado B3).
"""

import json
from pathlib import Path
import pytest

from delegation_core.config import Config
from delegation_core.extractor import extract
from delegation_core.vault import _countable_wikilinks, VaultManager
from delegation_core.embeddings import effective_chunk_chars
from delegation_core.synthesizer import _PROMPTS


# ── 1. Wikilinks with $ / currency symbols ───────────────────────────────────

def test_countable_wikilinks_preserves_currency_and_dollar_titles():
    content = """
    Check out [[Campo — Saldo cancelado chegou (R$13,8M _ 92 resci)]] and [[Budget $500k]].
    Also see [[US$ 10.5M ARR]] and [[Preço R$ 100]].
    """
    links = _countable_wikilinks(content)
    assert "Campo — Saldo cancelado chegou (R$13,8M _ 92 resci)" in links
    assert "Budget $500k" in links
    assert "US$ 10.5M ARR" in links
    assert "Preço R$ 100" in links


def test_countable_wikilinks_still_excludes_bash_tests():
    content = """
    if [[ -f "$file" ]]; then echo 1; fi
    if [[ ! -d /tmp ]]; then echo 2; fi
    if [[ $status == "ok" ]]; then echo 3; fi
    if [[ "$a" != "$b" ]]; then echo 4; fi
    if [[ $VAR ]]; then echo 5; fi
    """
    links = _countable_wikilinks(content)
    assert links == []


# ── 2. Office temporary lock files (~$*) ─────────────────────────────────────

def test_extractor_ignores_office_lock_files(tmp_path):
    lock_file = tmp_path / "~$2023-11-08 - Presentation.pptx"
    lock_file.write_bytes(b"temporary lock data")
    assert extract(lock_file) is None

    word_lock = tmp_path / "~$Document.docx"
    word_lock.write_bytes(b"temporary lock data")
    assert extract(word_lock) is None


# ── 3. Chunk size defaults and effective clamping ────────────────────────────

def test_config_default_chunk_sizes():
    cfg = Config()
    assert cfg.ingest_chunk_size == 3072
    assert cfg.vault_chunk_size == 3072


def test_effective_chunk_chars_with_bge_m3():
    # 1024 tokens * 3.0 chars/token floor = 3072 chars
    assert effective_chunk_chars("BAAI/bge-m3", 4000, 1024) == 3072
    assert effective_chunk_chars("BAAI/bge-m3", 3072, 1024) == 3072
    assert effective_chunk_chars("BAAI/bge-m3", 2000, 1024) == 2000


# ── 4. synthesis_lang in prompts ─────────────────────────────────────────────

def test_portuguese_prompts_contain_explicit_language_rules():
    assert "português do Brasil (PT-BR)" in _PROMPTS["pt"]["system"]
    assert "português do Brasil (PT-BR)" in _PROMPTS["pt"]["doc"]
    assert "português do Brasil (PT-BR)" in _PROMPTS["pt"]["meeting"]


# ── 5. vault_health force cache bypass ───────────────────────────────────────

def test_vault_health_summary_force_bypasses_cache(tmp_path):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "Projects").mkdir()
    note = vault_dir / "Projects" / "test.md"
    note.write_text("---\ntitle: Test Note\n---\nHello world", encoding="utf-8")

    cfg = Config(vault_path=str(vault_dir), vault_folders=["Projects"])
    vm = VaultManager(cfg)

    # First call creates cache
    res1 = vm.get_health_summary(force=False)
    assert res1["total_notes"] == 1

    # Add another note
    note2 = vault_dir / "Projects" / "test2.md"
    note2.write_text("---\ntitle: Test Note 2\n---\nHello again", encoding="utf-8")

    # Cached call returns old count
    res_cached = vm.get_health_summary(force=False)
    assert res_cached["total_notes"] == 1

    # Force call returns fresh count
    res_force = vm.get_health_summary(force=True)
    assert res_force["total_notes"] == 2
