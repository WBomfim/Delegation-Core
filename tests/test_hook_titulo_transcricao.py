"""Titulo da transcricao bruta: a primeira fala real, nao a marcacao do Claude Code."""

from __future__ import annotations

import importlib.util
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "src" / "delegation_core" / "hooks" / "session_export.py"


def _hook():
    spec = importlib.util.spec_from_file_location("session_export_titulo", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_titulo_pula_marcacao():
    h = _hook()
    msgs = [{"role": "user", "text": "<local-command-caveat>Caveat: The messages below</local-command-caveat>"},
            {"role": "user", "text": "<command-name>/model</command-name>"},
            {"role": "assistant", "text": "ok"},
            {"role": "user", "text": "palworld crashed after our <b>previous</b> modifications\nmore"}]
    assert h._topic(msgs) == "palworld crashed after our previous modifications"
    assert h._topic([{"role": "user", "text": "<x>"}]) == "Session"
    md = h._format_markdown([dict(m, ts="2026-08-27T19:36:00") for m in msgs], "abcdef1234", "/tmp")
    assert 'title: "Raw transcript - palworld crashed after our previous modifications"' in md
