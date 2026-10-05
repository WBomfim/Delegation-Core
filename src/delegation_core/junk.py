"""
junk.py — Boilerplate file detection.

Catches license files, READMEs, changelogs, requirements, and similar noise
before they reach the classifier or the vault. The model classifier has
repeatedly misfiled these into decisions/research — deterministic filtering
here is cheaper and more reliable than prompt-tuning.

Field deployment A introduced JUNK_STEM_RE to replace the original set-based approach.
"""

import re
from pathlib import Path

# Matches the filename stem (after stripping any chunk-staging prefix).
#
# O sufixo e `[._-]` mais UM token alfanumerico curto, nao `.*`. Essa e a forma
# de uma variante de boilerplate (`requirements-dev`, `LICENSE-MIT`,
# `CHANGELOG-2024`); nao e a forma de um titulo de nota.
#
# Com `.*` qualquer frase depois de um hifen entrava. Medido em 03/09/2026,
# nomes que um usuario deste vault escreveria e que eram DESCARTADOS:
#   todo-lista-do-projeto.md        install-do-cliente.md
#   changes-que-precisamos-fazer.md notice-de-reuniao.md
#   version-final-do-contrato.md    help-para-o-time.md
#   authors-do-artigo.md            manifest-de-entrega.md
# Todos sumiam do inbox para `_processed/` sem virar nota.
#
# O aperto troca um erro por outro, e a troca e deliberada: um
# `requirements-dev-test.txt` agora escapa e vira nota, enquanto antes um
# `install-do-cliente.md` era jogado fora. Arquivar um boilerplate a mais custa
# uma nota inutil no vault; descartar a nota de alguem custa a nota. Num sistema
# cujo proposito e ser a memoria permanente do usuario, o vies vai para o lado
# de nunca descartar.
JUNK_STEM_RE = re.compile(
    r"^(license|licence|copying|notice|readme|changelog|changes|contributing|"
    r"contributors|authors|requirements|install|installation|help|todo|"
    r"version|manifest|makefile|dockerfile|codeowners|gitignore|gitattributes|"
    r"editorconfig|pylintrc|flake8|mypy)([._\-][A-Za-z0-9]{1,12})?$",
    re.IGNORECASE,
)

# Office temporary lock files (prefix ~$).
_OFFICE_LOCK_RE = re.compile(r"^~\$")

# License/boilerplate content signals — matched against the first 500 chars.
_CONTENT_MARKERS = (
    "permission is hereby granted, free of charge",  # MIT
    "apache license",
    "gnu general public license",
    "bsd 2-clause",
    "bsd 3-clause",
    "mozilla public license",
    "creative commons",
)


def is_junk(filename: str, content: str = "") -> str | None:
    """Return a skip reason string if the file looks like boilerplate, else None.

    Checks filename stem first (fast path), then content markers if text is provided.
    """
    name = Path(filename).name
    # O ponto da frente sai antes de comparar. `Path(".gitignore").stem` e
    # ".gitignore", COM o ponto, entao seis entradas do padrao nunca casavam
    # nada: gitignore, gitattributes, editorconfig, pylintrc, flake8 e mypy sao
    # todas convencionalmente dotfiles. Estavam listadas como intencao e eram
    # inalcancaveis. Medido em 03/09/2026, e o organizer nao pula arquivo
    # oculto (`inbox.iterdir()` filtra so por is_file), entao um `.gitignore`
    # largado no inbox virava nota.
    stem = Path(filename).stem.lower().lstrip(".")

    if _OFFICE_LOCK_RE.match(name):
        return f"Office lock file ({name})"

    if JUNK_STEM_RE.match(stem):
        return f"matches boilerplate filename pattern ({stem})"

    if content:
        head = content[:500].lower()
        for marker in _CONTENT_MARKERS:
            if marker in head:
                return f"content matches license/boilerplate text ({marker!r})"

    return None
