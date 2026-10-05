"""
sidecar.py — Sidecar YAML metadata files for inbox ingestion.

A sidecar is a `<stem>.meta.yaml` file dropped alongside a main inbox file.
It can carry routing hints and content metadata that bypass the LLM classifier
and enrich the synthesis prompt.

Introduced in the field deployment B.

Supported sidecar keys:
  folder_hint   vault folder path to route to (bypasses classifier)
  no_merge      true → never merge this file into an existing note (field deployment A, ported from 0.1.0)
  type          document type hint for synthesis (meeting, research, decision, …)
  client        client/project name injected into the note frontmatter
  topics        list of topic tags
  council_session  (meeting-specific) council session identifier
"""

import logging
from pathlib import Path

logger = logging.getLogger("sidecar")

_SUFFIXES = (".meta.yaml", ".meta.yml")


def is_sidecar(path: Path) -> bool:
    """Return True if the path is a sidecar metadata file (not a main content file)."""
    return any(path.name.endswith(s) for s in _SUFFIXES)


def sidecar_for(main_path: Path) -> Path | None:
    """Return the sidecar path for main_path if one exists on disk, else None."""
    for suffix in _SUFFIXES:
        candidate = main_path.parent / f"{main_path.stem}{suffix}"
        if candidate.exists():
            return candidate
    return None


def load(main_path: Path) -> dict:
    """Load and parse the sidecar for main_path. Returns {} on missing or invalid YAML."""
    sc = sidecar_for(main_path)
    if not sc:
        return {}
    try:
        import yaml
        data = yaml.safe_load(sc.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("Failed to parse sidecar %s: %s", sc.name, e)
        return {}


def resolve_folder_hint(hint, vault_folders: list) -> str | None:
    """O `folder_hint` do sidecar, com a caixa do vault, ou None se invalido.

    Valida E canoniza numa chamada so, de proposito. A versao anterior era
    `is_valid_folder_hint`, devolvia bool, e o chamador em organizer.py usava o
    hint CRU como caminho (`folder = hint.strip("/")`). Separar as duas coisas
    e o que torna o defeito abaixo dificil de corrigir pela metade: tornar so a
    validacao insensivel a caixa faria um hint `sessions` criar uma pasta
    `sessions/` ao lado da `Sessions/` que o vault ja tem.

    O defeito: a comparacao era `head in vault_folders`, sensivel a caixa. A
    docstring de `config.resolve_folder` descreve exatamente esta armadilha —
    "code that hardcoded a lowercase name and tested `"sessions" in folders`
    silently did the wrong thing on such a vault" — e diz que os defaults de
    fabrica sao minusculos enquanto vaults configurados pelo wizard usam nomes
    capitalizados. Este modulo nunca adotou o helper.

    Medido nesta maquina em 03/09/2026, vault com `Sessions`, `Decisions`,
    `Reference`: os hints `sessions`, `decisions`, `sessions/2026` e
    `reference/x` eram TODOS rejeitados, e o arquivo caia no classificador do
    modelo em vez de ir para onde o sidecar mandou. Um roteamento explicito,
    ignorado em silencio.

    Subcaminho e preservado: `meetings/Nortex/2026-2027` continua valendo, e so
    o segmento raiz e canonizado.
    """
    if not hint or not isinstance(hint, str):
        return None
    partes = hint.strip("/").split("/", 1)
    from .config import resolve_folder
    raiz = resolve_folder(partes[0], vault_folders)
    if not raiz:
        return None
    return f"{raiz}/{partes[1]}" if len(partes) > 1 and partes[1] else raiz


def format_block(sidecar: dict) -> str:
    """Render sidecar as a bullet list for injection into a synthesis prompt.
    Excludes routing-only keys (folder_hint) that aren't content hints."""
    if not sidecar:
        return "(none)"
    skip_keys = {"folder_hint"}
    lines = []
    for k, v in sidecar.items():
        if k in skip_keys or v is None or v == "":
            continue
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        lines.append(f"- {k}: {v}")
    return "\n".join(lines) if lines else "(none)"
