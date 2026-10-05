"""
merger.py — Near-duplicate note merging.

Extracted from organizer.py in v0.2 so the merge policy is testable and
reusable independently of the main inbox pipeline.

Merge guards (field deployment A):
  _MAX_INCOMING  — documents larger than this are standalone artifacts
  _MAX_TARGET    — notes larger than this refuse incoming merges

Never-merge folders (field deployment B):
  sessions/ and meetings/ are chronological records; merging unrelated entries
  into them caused multiple sessions to pile up into one shared note.
"""

import logging

logger = logging.getLogger("merger")

_MAX_INCOMING = 32 * 1024    # characters of extracted text
_MAX_TARGET   = 150 * 1024   # bytes of existing note on disk

#: Pastas que nunca recebem merge, mesmo que a config nao as liste. Sao os
#: casos de campo que motivaram a guarda; a config acrescenta, nunca remove.
_NEVER_MERGE_FOLDERS = frozenset({"sessions", "meetings"})


def _never_merge(cfg) -> frozenset[str]:
    """As pastas protegidas de merge, minusculas, config mais os padroes.

    Duas coisas estavam erradas aqui e as duas eram silenciosas.

    **Caso.** A comparacao era `folder.split("/")[0] in _NEVER_MERGE_FOLDERS`,
    com o conjunto em minusculas. Esta maquina tem a pasta `Sessions` com
    maiuscula, entao a guarda NAO DISPARAVA: medido em 03/09/2026, nenhuma das
    nove pastas do vault era bloqueada. A guarda existe porque numa instalacao
    de campo o merge empilhou varias sessoes nao relacionadas numa nota so, que
    e o caso da implantacao B citado no topo deste modulo. `vault.py:1553` ja fazia isto
    certo, com um comentario dizendo exatamente que "a vault whose folder is
    'Sessions' would otherwise match nothing here". A licao foi aprendida la e
    nunca chegou aqui.

    **Config ignorada.** `cfg.never_merge_folders` existe, esta documentada em
    config.py e e lida por `vault.py`. Este modulo usava so a constante, entao
    quem configurasse a chave era obedecido no passe de heal e ignorado no
    merge de verdade.

    Uniao e nao substituicao: o default da config e `["sessions"]`, e trocar a
    constante por ela sozinha tiraria `meetings` da protecao sem ninguem pedir.
    """
    da_config = getattr(cfg, "never_merge_folders", None) or ["sessions"]
    return frozenset({f.lower() for f in da_config} | _NEVER_MERGE_FOLDERS)


def try_merge(
    vault_manager,
    hits: list[dict],
    raw_text: str,
    note_content: str,
    folder: str,
    filename: str,
    today: str,
) -> tuple[bool, str]:
    """Attempt to merge note_content into an existing near-duplicate.

    Returns (merged: bool, target_path: str).
    target_path is the vault-relative path merged into, or "" when not merged.

    Merge is skipped when:
      - the root folder is in _NEVER_MERGE_FOLDERS
      - raw_text exceeds _MAX_INCOMING (this is a standalone artifact)
      - the best matching existing note exceeds _MAX_TARGET
    """
    cfg = vault_manager.cfg
    root_folder = folder.split("/", 1)[0].lower()
    if root_folder in _never_merge(cfg):
        return False, ""

    if len(raw_text) > _MAX_INCOMING:
        return False, ""

    for hit in hits:
        if hit.get("similarity", 0) < cfg.merge_threshold:
            continue
        existing_path = cfg.vault / hit["path"]
        if not existing_path.exists():
            continue
        if existing_path.stat().st_size > _MAX_TARGET:
            logger.debug("Skipping merge into %s — target too large", hit["path"])
            continue

        existing_text = existing_path.read_text(encoding="utf-8")
        merged = (
            existing_text
            + f"\n\n---\n*Merged from `{filename}` — {today}*\n\n"
            + note_content
        )
        existing_path.write_text(merged, encoding="utf-8")
        vault_manager.index_note(
            merged,
            {"title": hit["title"], "path": hit["path"], "folder": hit.get("folder", folder)},
        )
        logger.info("Merged %s → %s", filename, hit["path"])
        return True, hit["path"]

    return False, ""
