"""
synthesizer.py — LLM-powered note synthesis.

v0.2: language-configurable via cfg.synthesis_lang ("en" | "pt").
  "en"  — English structured Obsidian notes (default)
  "pt"  — Portuguese prompts from the field deployment B

When cfg.synthesis_enabled is False the pipeline is bypassed and the organizer
files the raw extracted text directly (classify-only mode, equivalent to v0.1).

Input is capped at SYNTHESIS_INPUT_CHAR_LIMIT to stay within llama_ctx.
Uses engine.budget('synthesize') so CPU budget mode caps output tokens.
"""

import logging
import re
from pathlib import Path

from .vault import yaml_quote_scalar

logger = logging.getLogger("synthesizer")

SYNTHESIS_INPUT_CHAR_LIMIT = 24_000   # ~12k tokens at 2 chars/token for latin scripts

# ── English prompts ──────────────────────────────────────────────────────────

_EN_SYSTEM = (
    "You produce clean Obsidian Markdown notes. "
    "Output: Markdown only — no preamble, no comments, no surrounding code blocks."
)

_EN_DOC_PROMPT = """\
Synthesize the source text below into an Obsidian Markdown note.

OUTPUT: Markdown only. No preamble, no trailing explanation, no ```markdown``` wrappers.

FORMAT (follow literally — open AND close the frontmatter with --- on separate lines):

---
title: "<descriptive title extracted from content, always double-quoted>"
date: <YYYY-MM-DD from content, or ingestion date if unavailable>
type: <research | decision | reference | note>
client: <only if provided in sidecar>
tags: [<kebab-case-tags>]
---

## Summary
<continuous prose, max 150 words>

## Key Points
- <concrete specific bullet extracted from text>

## People
- <name and role, when text mentions them>

## Decisions / Actions
- [ ] <specific action + responsible party when identified>

RULES:
- Preserve ALL numbers, percentages, and monetary values exactly as in the text.
- If a section (People, Decisions) has no source material, OMIT it entirely. NEVER write placeholders.
- Do NOT copy these instructions or the sidecar metadata as note content.
- Do NOT write a closing paragraph explaining what you did.

Sidecar metadata (use ONLY to enrich the frontmatter — do not copy literally):
{sidecar_block}

File: {filename}
Format: {fmt}
Ingestion date: {date}

Source text:
{content}
"""

_EN_MEETING_PROMPT = """\
Synthesize this meeting / transcript into an Obsidian Markdown note.

OUTPUT: Markdown only. No preamble, no trailing explanation, no ```markdown``` wrappers.

FORMAT:

---
title: "<descriptive meeting title with date, always double-quoted>"
date: <YYYY-MM-DD of the meeting>
type: meeting
client: <only if provided in sidecar>
tags: [<kebab-case-tags>]
---

## Summary
<continuous prose, max 200 words>

## Attendees
- <Name (role when identified)>

## Results & Numbers
- <bullet with all financial data, metrics, percentages exactly as in text>

## Decisions & Actions
- [ ] <specific action with verb + responsible party when identified>

## Watch Points
- <risks, open questions, and alerts MENTIONED by participants>

## Key Quotes
- "<direct quote>" — <participant>

RULES:
- Preserve ALL numbers, percentages, and values exactly.
- Only include names that appear literally in the source text. Never invent names or roles.
- If a section has no source material, OMIT it entirely. Never write placeholders.
- Do NOT copy these instructions or sidecar metadata as note content.

Sidecar metadata:
{sidecar_block}

File: {filename}
Format: {fmt}
Ingestion date: {date}

Source text:
{content}
"""

# ── Portuguese prompts (field deployment B) ─────────────────────────────────

_PT_SYSTEM = (
    "Você produz notas Obsidian Markdown limpas em português do Brasil (PT-BR). Output: apenas Markdown — "
    "sem preâmbulo, sem comentários, sem blocos de código envoltórios."
)

_PT_DOC_PROMPT = """\
Sintetize o texto-fonte abaixo em uma nota Obsidian Markdown em português do Brasil (PT-BR).

OUTPUT: apenas o Markdown da nota. Sem preâmbulo, sem explicação posterior, sem comentários sobre a tarefa, sem blocos ```markdown``` envoltórios.

FORMATO (siga literalmente — abra e FECHE o frontmatter com --- em linhas separadas):

---
title: "<título descritivo extraído do conteúdo, sempre entre aspas duplas>"
date: <YYYY-MM-DD do conteúdo, ou data de ingestão se não houver>
type: <tipo: research / decision / reference / note>
client: <somente se informado no sidecar>
tags: [<tags-em-kebab-case>]
---

## Resumo
<prosa contínua, máximo 150 palavras>

## Tópicos Principais
- <bullet concreto e específico extraído do texto>

## Pessoas
- <nome e papel, quando o texto mencionar>

## Decisões / Ações
- [ ] <ação específica + responsável quando identificado>

REGRAS:
- Escreva a síntese estritamente em português do Brasil (PT-BR).
- Preserve TODOS os números, percentuais, valores em R$ exatamente como no texto.
- Se uma seção não tem matéria-prima, OMITA-a inteiramente. NUNCA escreva placeholders.
- NÃO copie estas instruções nem os metadados do sidecar como conteúdo da nota.
- NÃO escreva parágrafos finais explicando o que você fez.

Metadados do sidecar:
{sidecar_block}

Arquivo: {filename}
Formato: {fmt}
Data de ingestão: {date}

Texto-fonte:
{content}
"""

_PT_MEETING_PROMPT = """\
Sintetize esta reunião/ata em uma nota Obsidian Markdown em português do Brasil (PT-BR).

OUTPUT: apenas o Markdown da nota. Sem preâmbulo, sem explicação posterior, sem blocos ```markdown``` envoltórios.

FORMATO:

---
title: "<título descritivo da reunião com data, sempre entre aspas duplas>"
date: <YYYY-MM-DD da reunião>
type: meeting
client: <somente se informado no sidecar>
tags: [<tags-em-kebab-case>]
---

## Resumo
<prosa contínua, máximo 200 palavras>

## Participantes
- <Nome (papel quando identificado)>

## Resultados e Números
- <bullet com dados financeiros, métricas, percentuais exatamente como no texto>

## Decisões e Ações
- [ ] <ação específica + responsável se identificado>

## Pontos de Atenção
- <riscos, alertas e temas em aberto>

## Trechos Relevantes
- "<citação direta>" — <participante>

REGRAS:
- Escreva a síntese estritamente em português do Brasil (PT-BR).
- Preserve TODOS os números exatamente.
- NUNCA invente nomes ou papéis.
- Se uma seção não tem matéria-prima, OMITA-a. NUNCA escreva placeholders.

Metadados do sidecar:
{sidecar_block}

Arquivo: {filename}
Formato: {fmt}
Data de ingestão: {date}

Texto-fonte:
{content}
"""

_PROMPTS: dict[str, dict[str, str]] = {
    "en": {"system": _EN_SYSTEM, "doc": _EN_DOC_PROMPT, "meeting": _EN_MEETING_PROMPT},
    "pt": {"system": _PT_SYSTEM, "doc": _PT_DOC_PROMPT, "meeting": _PT_MEETING_PROMPT},
}

# ── post-processing ──────────────────────────────────────────────────────────

_TRAILING_CHATTER_RE = re.compile(
    r"\n\n+(?:Este[ ]é|Esta[ ]é|Aqui[ ]está|Note[ ]que|Observe[ ]que|"
    r"This[ ]is|Here[ ]is|Note[ ]that)[^\n].*$",
    re.IGNORECASE | re.DOTALL,
)

_PROMPT_LEAK_PATTERNS = [
    re.compile(r"\n+(?:REGRAS|RULES|FORMATO|OUTPUT)\s*:\s*\n.*$", re.DOTALL | re.IGNORECASE),
    re.compile(r"\n+(?:Metadados do sidecar|Sidecar metadata).*$", re.DOTALL | re.IGNORECASE),
    re.compile(r"\n+(?:Arquivo|File|Formato|Format|Data de ingestão|Ingestion date)\s*:.*\n.*$", re.DOTALL | re.IGNORECASE),
    re.compile(r"\n+(?:Texto[- ]fonte|Source text)\s*:\s*\n.*$", re.DOTALL | re.IGNORECASE),
    re.compile(r"\n+\[Page\s+\d+\].*$", re.DOTALL | re.IGNORECASE),
    re.compile(r"\n+##+ +Metadados.*$", re.DOTALL | re.IGNORECASE),
]

# Frontmatter fields the LLM fills with arbitrary free text (as opposed to
# controlled vocab like type/date) — any of these can contain a raw ": " and
# break YAML if left unquoted.
_FREE_TEXT_FIELDS = ("title", "client")


def _quote_frontmatter_fields(content: str) -> str:
    """Force-quote free-text fields inside the leading frontmatter block.

    The prompt asks the model to double-quote these, but LLM output isn't
    reliable — an unquoted value containing ": " is invalid YAML that breaks
    Obsidian's frontmatter parser. This is the safety net regardless of what
    the model actually produced.
    """
    if not content.startswith("---\n"):
        return content
    close = content.find("\n---\n", 4)
    if close == -1:
        return content
    fm = content[4:close]

    for field in _FREE_TEXT_FIELDS:
        line_re = re.compile(rf"^{field}:[ \t]*(.*)$", re.MULTILINE)

        def _requote(m: re.Match) -> str:
            raw = m.group(1).strip()
            if not raw:
                return m.group(0)
            if len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]:
                return m.group(0)
            return f"{field}: {yaml_quote_scalar(raw)}"

        fm = line_re.sub(_requote, fm, count=1)

    return content[:4] + fm + content[close:]


_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_UNCLOSED_THINK_RE = re.compile(r"<think>.*", re.DOTALL | re.IGNORECASE)


def _strip_think_tags(raw: str) -> str:
    """Strip reasoning-model <think>...</think> blocks before any other processing.

    Some local models (reasoning-tuned variants) emit their chain-of-thought
    wrapped in <think> tags. If synthesis output starts with or contains one,
    nothing downstream strips it — it leaks straight into the note (often into
    a frontmatter field), corrupting content and usually breaking YAML too
    since reasoning text is full of colons. Handles a dangling unclosed
    <think> (output truncated mid-reasoning) by dropping everything from the
    tag onward.
    """
    cleaned = _THINK_BLOCK_RE.sub("", raw)
    if "<think>" in cleaned.lower():
        cleaned = _UNCLOSED_THINK_RE.sub("", cleaned)
    return cleaned.strip()


_PLACEHOLDER_RE = re.compile(
    r"\(.*?(?:não\s+mencionad|nao\s+mencionad|não\s+especificad|"
    r"a\s+definir|a\s+identificar|a\s+confirmar|a\s+ser\s+|"
    r"N/A|n\.a\.|to be defined|to be identified|not mentioned|TBD).*?\)",
    re.IGNORECASE,
)


def _drop_placeholder_sections(content: str) -> str:
    parts = re.split(r"(^##+ [^\n]+$)", content, flags=re.MULTILINE)
    if len(parts) <= 1:
        return content
    out = [parts[0]]
    i = 1
    while i < len(parts):
        heading, body = parts[i], (parts[i + 1] if i + 1 < len(parts) else "")
        non_empty = [l.strip() for l in body.split("\n") if l.strip()]
        if non_empty and not all(_PLACEHOLDER_RE.search(l) for l in non_empty):
            out.extend([heading, body])
        i += 2
    return "".join(out)


def sanitize_note(raw: str) -> str:
    """Strip reasoning blocks, code fences, echoed prompt scaffolding, trailing chatter, and placeholder sections."""
    raw = _strip_think_tags(raw)
    lines = [l for l in raw.split("\n") if not re.match(r"^\s*```", l)]
    cleaned = "\n".join(lines).strip()
    for pattern in _PROMPT_LEAK_PATTERNS:
        cleaned = pattern.sub("", cleaned).strip()
    cleaned = _TRAILING_CHATTER_RE.sub("", cleaned).strip()
    cleaned = _drop_placeholder_sections(cleaned).strip()
    cleaned = re.sub(
        r"(^---\n.*?^---\n)\s*---\n.*?^---\n", r"\1",
        cleaned, count=1, flags=re.MULTILINE | re.DOTALL,
    )
    if cleaned.startswith("---\n"):
        body = cleaned[4:]
        if not re.search(r"^---\s*$", body, re.MULTILINE):
            heading = re.search(r"^##\s", body, re.MULTILINE)
            if heading:
                insert_at = 4 + heading.start()
                cleaned = cleaned[:insert_at].rstrip() + "\n---\n\n" + cleaned[insert_at:]
            else:
                cleaned = cleaned.rstrip() + "\n---\n"
    cleaned = _quote_frontmatter_fields(cleaned)
    return cleaned


def _is_meeting_like(sidecar: dict, filename: str, content: str) -> bool:
    from .classifier import looks_like_session
    stype = str((sidecar or {}).get("type", "")).lower()
    if stype in ("meeting", "transcript", "ata", "minutes"):
        return True
    return looks_like_session(filename, content)


async def synthesize(engine, sidecar: dict, content: str, filename: str, fmt: str, today: str) -> str:
    """Generate a structured Obsidian note from raw text.

    Uses cfg.synthesis_lang to select prompt language.
    Falls back to a minimal raw-content dump on synthesis failure.
    """
    lang = getattr(engine.cfg, "synthesis_lang", "en")
    prompts = _PROMPTS.get(lang, _PROMPTS["en"])

    from .sidecar import format_block
    truncated = len(content) > SYNTHESIS_INPUT_CHAR_LIMIT
    text_input = content[:SYNTHESIS_INPUT_CHAR_LIMIT]

    prompt_tmpl = prompts["meeting"] if _is_meeting_like(sidecar, filename, content) else prompts["doc"]
    prompt = prompt_tmpl.format(
        sidecar_block=format_block(sidecar or {}),
        filename=filename,
        fmt=fmt,
        date=today,
        content=text_input,
    )

    try:
        raw = await engine.invoke(
            prompt,
            system=prompts["system"],
            # A carga e o TEXTO-FONTE, e nao um pedaco do gabarito. Sem isto, o
            # modo agente cortava o prompt no primeiro \n\n e devolvia o
            # paragrafo "OUTPUT: Markdown only. No preamble..." como se fosse a
            # nota: duas notas deste vault comecam exatamente assim.
            fallback_payload=text_input,
            max_tokens=engine.budget("synthesize", 2500),
            temperature=0.2,
            task="synthesize",
        )
        note = sanitize_note(raw)
        if not note.strip():
            raise RuntimeError("synthesis returned empty content")
    except Exception as e:
        logger.warning("Synthesis failed for %s: %s — falling back to raw text", filename, e)
        title = Path(filename).stem
        client = (sidecar or {}).get("client", "")
        client_line = f"\nclient: {yaml_quote_scalar(client)}" if client else ""
        note = (
            f"---\ntitle: {yaml_quote_scalar(title)}\ndate: {today}\nsynthesis: failed{client_line}\n---\n\n"
            f"## Raw Content\n\n{content[:SYNTHESIS_INPUT_CHAR_LIMIT]}"
        )

    if truncated:
        note += (
            f"\n\n---\n*Source truncated at {SYNTHESIS_INPUT_CHAR_LIMIT} chars "
            f"(of {len(content)}). Consider increasing llama_ctx or chunked synthesis.*\n"
        )
    return note
