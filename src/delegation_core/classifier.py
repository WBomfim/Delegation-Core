"""
classifier.py — LLM-based inbox routing.

v0.2: uses engine.budget('classify') so the 8-token cap is enforced automatically
on CPU hardware. FOLDER_HINTS (field deployment A) injected into the prompt. Fallback goes to
'reference' (neutral), never to folders[0] which may be semantically loaded.
"""

import logging
import re

from .config import resolve_folder

logger = logging.getLogger("classifier")

FOLDER_HINTS: dict[str, str] = {
    "decisions":      "choices made, strategies adopted, commitments, architectural decisions",
    "research":       "investigations, analyses, findings, studies, evaluations",
    "insights":       "lessons learned, observations, patterns, retrospectives",
    "fixes":          "bug fixes, workarounds, error solutions, debugging outcomes",
    "tools":          "reusable scripts, utilities, automations, prompts",
    "sessions":       "meeting notes, conversation logs, session transcripts, standups",
    "meetings":       "meeting agendas, minutes, attendees, action items",
    "reference":      "reference material, documentation, external knowledge, specs",
    "procedures":     "step-by-step guides, how-tos, runbooks, checklists",
    "infrastructure": "system setup, deployment, server configuration, DevOps notes",
    "projects":       "project plans, briefs, scopes, roadmaps",
    "scratch":        "drafts, temporary notes, work in progress, unsorted",
}

_SESSION_MARKERS = (
    "session synopsis",
    "session summary",
    "session transcript",
)


def looks_like_session(filename: str, content: str) -> bool:
    """Deterministic fast-path: true when the file is clearly a session log."""
    stem = filename.lower()
    if stem.startswith("session") or "session-" in stem or "session_" in stem:
        return True
    head = content[:500].lower()
    if any(m in head for m in _SESSION_MARKERS):
        return True
    return bool(re.search(r"^tags:.*\bsession\b", content[:500], re.MULTILINE | re.IGNORECASE))


async def classify(engine, folders: list[str], filename: str, content: str, fmt: str = "Text") -> str:
    """Classify a document into one of folders via a single llama.cpp call.

    Uses engine.budget('classify') so CPU budget mode caps tokens to 8.
    Falls back to 'reference' on model error or invalid response.
    """
    # resolve_folder, not `in folders`: a vault using Capitalized folder names
    # ("Sessions", "Reference") would otherwise miss every one of these lookups.
    sessions_folder = resolve_folder("sessions", folders)
    if sessions_folder and looks_like_session(filename, content):
        return sessions_folder

    fallback = resolve_folder("reference", folders) or folders[0]

    hint_lines = [f"  {f}: {FOLDER_HINTS[f.lower()]}" for f in folders
                  if f.lower() in FOLDER_HINTS]
    folder_block = "\n".join(hint_lines) if hint_lines else ", ".join(folders)

    prompt = (
        f"Classify this {fmt} document into exactly one folder.\n"
        f"Reply with ONLY the folder name — no punctuation, no explanation.\n"
        f"Folders:\n{folder_block}\n\n"
        f"Filename: {filename}\n"
        f"Content:\n{content[:800]}"
    )
    try:
        result = await engine.invoke(
            prompt,
            system="Vault Classifier",
            max_tokens=engine.budget("classify", 8),
            temperature=0.1,
            task="classify",
            # Este chamador ja era seguro -- valida a resposta contra a lista de
            # pastas e cai no fallback quando nao casa -- mas declarar a carga
            # troca "seguro por acidente" por "seguro por construcao", e tira o
            # aviso de chamador esquecido do log.
            fallback_payload=fallback,
        )
        candidate = re.sub(r"[^a-z0-9_/-]", "", result.strip().lower().split()[0])
        # The candidate is lowercased above, so it can only ever match a folder
        # list that is itself lowercase — resolve it back to the real folder name.
        return resolve_folder(candidate, folders) or fallback
    except Exception as e:
        logger.warning("Classification failed for %s: %s — using %s", filename, e, fallback)
        return fallback
