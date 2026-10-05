"""
splitter.py — Three-tier recursive note splitting for large inbox files (v0.3).

When a file exceeds cfg.split_min_chars or is a multi-page PDF, should_split()
returns a list of (title, content) sections instead of a single blob.

Tier 1 (zero inference cost): structural
  - PDFs  → page-grouped sections via extract_pages()
  - .md/.txt → heading-based split (H1/H2 via regex)

Tier 2 (zero inference cost): size-based
  - Any format ≥ split_min_chars without detectable structure →
    paragraph-boundary chunking

Tier 3 (not in splitter — handled by organizer): LLM title upgrade
  - organizer.run() calls engine.invoke() on anonymous section titles
    ("Section N") only; actual content is never rewritten by the LLM.

inject_sibling_links() cross-links all notes produced from a single source
file by appending [[sibling]] wikilinks under ## Related in each note.
This runs after all sections are filed so every sibling path is known.
"""

import logging
import math
import re
from pathlib import Path

from .extractor import extract_pages
from .linker import add_related_links, existing_targets, format_link

logger = logging.getLogger("splitter")

_HEADING_RE = re.compile(r"^(#{1,2})\s+(.+)$", re.MULTILINE)


# ── Tier-1: structural splits ─────────────────────────────────────────────────

def _split_by_headings(text: str) -> list[tuple[str, str]]:
    """Split markdown/plain text by H1/H2 headings. Returns [] if < 2 found."""
    matches = list(_HEADING_RE.finditer(text))
    if len(matches) < 2:
        return []

    sections: list[tuple[str, str]] = []
    preamble = text[:matches[0].start()].strip()
    if preamble:
        sections.append(("Introduction", preamble))

    for i, m in enumerate(matches):
        title = m.group(2).strip()
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            sections.append((title, body))

    return sections


def _split_pdf_pages(pages: list[str], max_notes: int) -> list[tuple[str, str]]:
    """Group PDF pages into at most max_notes sections."""
    total = len(pages)
    group_size = math.ceil(total / max_notes)
    sections: list[tuple[str, str]] = []
    for i in range(0, total, group_size):
        group = pages[i:i + group_size]
        start, end = i + 1, min(i + group_size, total)
        title = f"Page {start}" if start == end else f"Pages {start}–{end}"
        sections.append((title, "\n\n".join(group)))
    return sections


# ── Tier-2: size-based split ──────────────────────────────────────────────────

def _paragraph_chunks(text: str, max_chars: int, max_sections: int) -> list[tuple[str, str]]:
    """Split text at paragraph boundaries near max_chars.

    Returns [] when the text fits in a single chunk (no split needed).
    """
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paragraphs) <= 1:
        return []

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for para in paragraphs:
        fits_in_current = current_len + len(para) <= max_chars
        budget_for_more = len(chunks) < max_sections - 1

        if not fits_in_current and current and budget_for_more:
            chunks.append("\n\n".join(current))
            current = [para]
            current_len = len(para)
        else:
            current.append(para)
            current_len += len(para) + 2  # +2 for \n\n separator

    if current:
        chunks.append("\n\n".join(current))

    return [] if len(chunks) <= 1 else [
        (f"Section {i + 1}", chunk) for i, chunk in enumerate(chunks)
    ]


# ── Decision function ─────────────────────────────────────────────────────────

def should_split(raw_text: str, src: Path, cfg) -> list[tuple[str, str]]:
    """Return (title, content) sections if this file warrants recursive splitting.

    Returns [] if the file should be processed as a single note.

    Decision order:
      1. PDFs with > 1 non-blank page — page-grouped split (always attempted)
      2. Text ≥ split_min_chars with H1/H2 headings — heading split
      3. Any format ≥ split_min_chars — paragraph-boundary chunking
    """
    min_chars = getattr(cfg, "split_min_chars", 3000)
    max_notes = getattr(cfg, "split_max_notes", 10)

    # Tier 1a: PDF page split (always attempted for PDFs)
    pages = extract_pages(src)
    if pages and len(pages) > 1:
        sections = _split_pdf_pages(pages, max_notes)
        logger.info("%s: PDF split → %d sections (%d pages)", src.name, len(sections), len(pages))
        return sections

    # Tier 1b + 2: text-based splits require reaching the size threshold
    if len(raw_text) < min_chars:
        return []

    suffix = src.suffix.lower()
    if suffix in (".md", ".markdown", ".txt", ".text"):
        heading_sections = _split_by_headings(raw_text)
        if len(heading_sections) >= 2:
            if len(heading_sections) > max_notes:
                # Merge overflow into the last slot rather than silently dropping content.
                # Paragraph and PDF paths both preserve all content; heading path must too.
                overflow = heading_sections[max_notes - 1:]
                merged = (overflow[0][0], "\n\n".join(c for _, c in overflow))
                heading_sections = heading_sections[:max_notes - 1] + [merged]
                logger.info("%s: heading split → %d sections (merged %d overflow into last slot)",
                            src.name, max_notes, len(overflow))
            else:
                logger.info("%s: heading split → %d sections", src.name, len(heading_sections))
            return heading_sections

    chunks = _paragraph_chunks(raw_text, min_chars, max_notes)
    if chunks:
        logger.info("%s: paragraph split → %d chunks", src.name, len(chunks))
        return chunks

    return []


# ── Sibling cross-linking ─────────────────────────────────────────────────────

def inject_sibling_links(vault_manager, note_paths: list[str], cfg) -> int:
    """Cross-link all notes in note_paths with [[sibling]] wikilinks under ## Related.

    note_paths: vault-relative paths of the newly created sibling notes.
    Strictly additive — never removes existing links.
    Returns count of notes that were updated.
    """
    if len(note_paths) < 2:
        return 0

    # Resolve (stem, rel_path, abs_path) for all siblings that exist on disk
    siblings: list[tuple[str, str, Path]] = []
    for rel in note_paths:
        abs_path = cfg.vault / rel
        if abs_path.exists():
            siblings.append((abs_path.name[:-3], rel, abs_path))

    updated = 0
    for stem, rel, abs_path in siblings:
        try:
            content = abs_path.read_text(encoding="utf-8")
            already = existing_targets(content)
            new_links = [
                f"- {format_link(s)}"
                for s, r, _ in siblings
                if r != rel and s not in already
            ]
            if not new_links:
                continue
            updated_content = add_related_links(content, new_links)
            abs_path.write_text(updated_content, encoding="utf-8")
            vault_manager.index_note(updated_content, {
                "title": stem,
                "path":  rel,
                "folder": abs_path.parent.relative_to(cfg.vault).as_posix(),
            })
            updated += 1
        except Exception as e:
            logger.warning("inject_sibling_links: %s — %s", rel, e)

    return updated
