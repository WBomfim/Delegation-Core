#!/usr/bin/env python3
"""
session_export.py — Claude Code SessionEnd hook.

Fires automatically when a Claude Code session closes.
Reads the session transcript JSONL, formats it as markdown,
and writes it to the vault's configured Sessions folder.

This is a raw-transcript backup. It is NOT a replacement for
export_session() (the MCP tool Claude calls to write a curated summary).
Both files are useful: this one is the full record, the MCP tool is the digest.

Only stdlib, so the hook never pays for the rest of the package's imports.
Runs as part of `delegation-core-hook session-end` (see entrada.py), which the
installer registers in ~/.claude/settings.json.
"""

import json
import platform
import re
import subprocess
import sys
import os
from datetime import datetime
from pathlib import Path

CONFIG_PATH = Path.home() / ".delegation_core" / "config.json"
MAX_TEXT_LENGTH = 8000  # truncate very long individual messages


def _yaml_quote_scalar(value: str) -> str:
    """Double-quote a string for safe use as a YAML frontmatter scalar value.

    An unquoted scalar containing ": " (colon-space) is ambiguous/invalid YAML
    (Obsidian and any strict frontmatter parser will choke on it) — quote
    unconditionally so titles are safe regardless of content. Duplicated from
    delegation_core.notes.yaml_quote_scalar rather than imported: the hooks
    stay stdlib-only, so a session end never pays for the package's imports.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'

_VENV_DIR = Path.home() / ".delegation_core" / "venv"
VENV_BIN = (
    _VENV_DIR / "Scripts" / "delegation-core.exe"
    if platform.system() == "Windows"
    else _VENV_DIR / "bin" / "delegation-core"
)
REINDEX_LOG = Path.home() / ".delegation_core" / "reindex.log"


def _detached_popen_kwargs() -> dict:
    """Platform-appropriate kwargs to fully detach a background process.

    POSIX: ``start_new_session`` (setsid) so the child survives the parent's
    exit. Windows: ``start_new_session`` is silently ignored by subprocess
    on that platform, so use DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP
    instead, which achieve the equivalent (no console, own process group).
    """
    if platform.system() == "Windows":
        return {
            "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        }
    return {"start_new_session": True}


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _resolve_sessions_dir(vault: Path, config: dict) -> Path:
    """Pick the vault's session folder using its configured casing.

    Hardcoding ``vault / "sessions"`` created a second, lowercase folder beside
    the configured ``Sessions/`` on every vault set up with Capitalized names —
    and because indexing, health accounting and search all iterate
    ``vault_folders``, every transcript written there was invisible: not in
    ChromaDB, not searchable, not counted. Resolved case-insensitively against
    the configured list, mirroring delegation_core.config.resolve_folder, which
    this stdlib-only hook cannot import.
    """
    for folder in config.get("vault_folders") or []:
        if isinstance(folder, str) and folder.strip().lower() == "sessions":
            return vault / folder.strip()
    return vault / "Sessions"


def _trigger_reindex() -> bool:
    """Fire `delegation-core reindex` as a detached background process so the
    transcript just written becomes searchable without a manual reindex.

    Since v0.11 that command hands the work to the running HTTP daemon rather
    than opening its own ChromaDB, so this no longer starts a second writer
    against an index the daemon holds open, nor a second copy of BGE on the GPU
    — see daemon.py. It still does the work in-process when no daemon answers,
    which is what keeps this hook working on a machine without the service.

    Guarded: skips immediately if the `no_auto_reindex` sentry file exists.
    Best-effort: returns False if the venv binary isn't installed yet.
    """
    if not VENV_BIN.exists():
        return False
    if (Path.home() / ".delegation_core" / "no_auto_reindex").exists():
        return False
    try:
        with open(REINDEX_LOG, "a", encoding="utf-8") as log_fh:
            subprocess.Popen(
                [str(VENV_BIN), "reindex"],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                **_detached_popen_kwargs(),
            )
        return True
    except Exception:
        return False


def _parse_transcript(transcript_path: str) -> list[dict]:
    """Extract user and assistant text turns from the JSONL transcript."""
    messages = []
    try:
        with open(transcript_path, encoding="utf-8") as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                msg_type = obj.get("type")
                if msg_type not in ("user", "assistant"):
                    continue

                message = obj.get("message", {})
                role = message.get("role", msg_type)
                content = message.get("content", "")
                ts = obj.get("timestamp", "")

                if isinstance(content, str):
                    text = content.strip()
                elif isinstance(content, list):
                    # Keep only text blocks; skip thinking, tool_use, tool_result
                    parts = [
                        c.get("text", "")
                        for c in content
                        if isinstance(c, dict) and c.get("type") == "text"
                    ]
                    text = "\n".join(parts).strip()
                else:
                    continue

                if not text:
                    continue

                if len(text) > MAX_TEXT_LENGTH:
                    text = text[:MAX_TEXT_LENGTH] + "\n\n*[truncated — full text in source transcript]*"

                messages.append({"role": role, "text": text, "ts": ts})

    except Exception as e:
        sys.stderr.write(f"session_export: failed to read transcript: {e}\n")

    return messages


def _session_date(messages: list[dict]) -> str:
    """The date the session STARTED, from its first timestamped message.

    Not "today": a session resumed across midnight, or exported the morning
    after, would otherwise be dated by when the export ran rather than by when
    the conversation happened — and, while the filename carried that date, it
    also produced a second note for a session already in the vault.
    """
    for m in messages:
        ts = (m.get("ts") or "")[:10]
        if len(ts) == 10 and ts[4] == "-":
            return ts
    return datetime.now().strftime("%Y-%m-%d")


# Credential shapes that must never reach the vault. The transcript is written
# to disk in plain text and then indexed for semantic search, so a token pasted
# into a conversation would otherwise sit there readable, searchable and
# retrievable by every future session, forever.
#
# Not hypothetical: a ClickUp personal token was pasted into a session on
# 02/09/2026 and this very hook would have persisted it. The redaction was
# written into the installed copy that same day and never made it back into the
# repository, so every fresh install kept shipping the version without it.
#
# Deliberately dumb pattern matching. It is a floor, not a guarantee: it cannot
# recognise a secret that has no distinctive shape, so a credential that reaches
# a transcript should still be rotated. Redacting the formatted text (rather
# than each message) means anything the formatter copies into frontmatter or a
# title is covered too.
_SEGREDOS = [
    (re.compile(r"\bpk_\d+_[A-Z0-9]{20,}\b"), "[TOKEN CLICKUP REMOVIDO]"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"), "[CHAVE ANTHROPIC REMOVIDA]"),
    (re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"), "[CHAVE OPENAI REMOVIDA]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), "[TOKEN GITHUB REMOVIDO]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"), "[TOKEN SLACK REMOVIDO]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[CHAVE AWS REMOVIDA]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
     "[JWT REMOVIDO]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                re.S), "[CHAVE PRIVADA REMOVIDA]"),
]


def _redigir(texto):
    """Strip credential-shaped strings. Returns the text and how many it removed.

    The count is returned rather than discarded because silent redaction teaches
    nobody anything: a transcript that had a token in it is a transcript whose
    token needs rotating, and the person can only act on that if the note says
    so.
    """
    total = 0
    for rx, marca in _SEGREDOS:
        texto, n = rx.subn(marca, texto)
        total += n
    return texto, total


_TAG = re.compile(r"<[^>]{1,80}>")


def _topic(messages: list[dict]) -> str:
    """The first real line a person typed, without Claude Code's markup.

    Measured 27/09/2026: 40 of 75 raw transcripts were titled
    "<local-command-caveat>Caveat: The messages below...", the tag Claude Code
    puts before a local command, because that was the first "user" message.
    """
    for m in messages:
        if m.get("role") != "user":
            continue
        texto = (m.get("text") or "").strip()
        if not texto or texto.startswith("<"):
            continue
        linha = next((l.strip() for l in texto.splitlines() if l.strip()), "")
        linha = re.sub(r"\s+", " ", _TAG.sub(" ", linha)).strip()
        if linha:
            return linha[:60]
    return "Session"


def _format_markdown(
    messages: list[dict], session_id: str, cwd: str, segredos_removidos: int = 0,
) -> str:
    date_str = _session_date(messages)
    time_str = datetime.now().strftime("%H:%M")
    short_id = session_id[:8]

    topic = _topic(messages)

    lines = [
        "---",
        # Hyphen and not an em dash: the vault's writing rule forbids it, and
        # every transcript this hook writes lands in the vault.
        f"title: {_yaml_quote_scalar(f'Raw transcript - {topic}')}",
        f"date: {date_str}",
        f"exported_at: {datetime.now().isoformat(timespec='seconds')}",
        f"session_id: {session_id}",
        f"cwd: {cwd}",
        f"messages: {len(messages)}",
        "type: session-transcript",
    ]
    if segredos_removidos:
        lines.append(f"segredos_removidos: {segredos_removidos}")
    lines.extend([
        "---",
        "",
        f"# Session transcript — {short_id}",
        f"*Exported automatically at {date_str} {time_str}*  ",
        f"*Working directory: `{cwd}`*",
        "",
        "> This is a verbatim raw transcript. For the curated summary, see the",
        "> `export_session` note written by Claude before ending the session.",
        "",
        "---",
        "",
    ])

    for msg in messages:
        role_label = "### You" if msg["role"] == "user" else "### Claude"
        ts_label = f" *({msg['ts'][:16].replace('T', ' ')})*" if msg["ts"] else ""
        lines.append(f"{role_label}{ts_label}")
        lines.append("")
        lines.append(msg["text"])
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def main(raw: str | None = None) -> int:
    """Exporta a transcricao da sessao que terminou. `raw` e o JSON do hook;
    sem ele, le da entrada padrao. Devolve 0 sempre: o fim da sessao nunca
    pode falhar por causa do export."""
    if raw is None:
        raw = sys.stdin.read()
    raw = raw.strip()
    if not raw:
        return 0

    try:
        hook_data = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"session_export: bad hook JSON: {e}\n")
        return 0

    transcript_path = hook_data.get("transcript_path", "")
    session_id = hook_data.get("session_id", "unknown")
    cwd = hook_data.get("cwd", "")

    if not transcript_path or not Path(transcript_path).exists():
        sys.stderr.write(f"session_export: no transcript at '{transcript_path}'\n")
        return 0

    config = _load_config()
    vault_path = config.get("vault_path", "")
    if not vault_path:
        sys.stderr.write("session_export: delegation-core not configured — skipping export\n")
        return 0

    vault = Path(vault_path).expanduser()
    sessions_dir = _resolve_sessions_dir(vault, config)

    try:
        sessions_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        sys.stderr.write(f"session_export: could not create {sessions_dir.name}/ dir: {e}\n")
        return 0

    messages = _parse_transcript(transcript_path)
    if not messages:
        sys.stderr.write("session_export: no messages found in transcript — skipping\n")
        return 0

    short_id = session_id[:8]
    # The session id alone — no date prefix. The name has to be STABLE across
    # exports of the same session, and a date makes it change the moment a
    # session is resumed on another day. Combined with the old "skip if it
    # exists" guard, that produced a fresh partial copy per day instead of one
    # note per session: a field install had 111 transcripts for 47 real
    # sessions, 891 duplicate chunks competing in every search. The date lives
    # in frontmatter, where it can be the session's own start date.
    filename = f"transcript-{short_id}.md"
    dest = sessions_dir / filename

    # No existence guard: re-exporting the same session must REPLACE its note,
    # since the later export is the more complete one. The write below is atomic
    # (.tmp + os.replace), so a crash mid-write cannot truncate the existing
    # note either.
    # Redact each message before building the markdown so that truncating the
    # title at 60 chars never slices a secret across its pattern boundary.
    total_removidos = 0
    redacted_messages = []
    for msg in messages:
        txt, n = _redigir(msg.get("text", ""))
        total_removidos += n
        m_copy = dict(msg)
        m_copy["text"] = txt
        redacted_messages.append(m_copy)

    content = _format_markdown(
        redacted_messages, session_id, cwd, segredos_removidos=total_removidos,
    )
    try:
        tmp = dest.with_suffix(".tmp")
        # Redact BEFORE the first write: the .tmp file is on the same disk and
        # would hold the secret in cleartext for the moment it existed.
        content, extra = _redigir(content)
        if extra and "segredos_removidos:" not in content:
            content = content.replace(
                "type: session-transcript",
                f"type: session-transcript\nsegredos_removidos: {total_removidos + extra}", 1)
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, dest)
        sys.stderr.write(
            f"session_export: saved {len(messages)} messages ({len(content)} chars) → {dest}\n"
        )
    except Exception as e:
        sys.stderr.write(f"session_export: write failed: {e}\n")
        return 0

    if _trigger_reindex():
        sys.stderr.write("session_export: triggered background reindex\n")
    return 0
