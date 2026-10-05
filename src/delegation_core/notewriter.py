"""notewriter.py — the single path by which a note reaches the vault.

Extracted when the dashboard gained editing. Before that, every write went
through server.py's MCP tools, so the file-write / index / wikilink-injection
sequence lived there as a private helper. Giving the dashboard its own copy
would have created a second write path free to drift from the first — which is
the exact failure this codebase spent a day removing elsewhere (two wikilink
parsers disagreeing, two definitions of "generated", a labeler nothing called).

So both surfaces call these functions instead. `vault` is passed in rather than
read from a module global, because server.py and dashboard_api.py each hold
their own VaultManager.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from .linker import inject_backlinks, wikilinks
from .vault import (
    compose_note,
    resolve_in_vault,
    resolve_vault_folder,
    safe_filename,
    unique_note_path,
    yaml_quote_scalar,
)

logger = logging.getLogger("notewriter")


def post_write_links(vault, note_path: Path, rel_path: str, folder: str, stem: str) -> None:
    """Inject forward wikilinks + backlinks after a vault write.

    BGE search only — no llama.cpp call, fast enough to run inline. Also drops
    the vault_health.json cache so heartbeat() stops reporting pre-write numbers.
    """
    try:
        content = note_path.read_text(encoding="utf-8")
        hits = [h for h in vault.search(content[:600], limit=6)
                if h.get("path") != rel_path][:5]
        links = wikilinks(hits, vault.cfg.merge_threshold, vault.cfg.vault)
        if links:
            updated = content.rstrip() + f"\n\n## Related\n{links}\n"
            note_path.write_text(updated, encoding="utf-8")
            vault.index_note(updated, {"title": stem, "path": rel_path, "folder": folder})
            inject_backlinks(vault, stem,
                             [h["path"] for h in hits
                              if h.get("similarity", 0) >= vault.cfg.merge_threshold])
    except Exception as e:
        logger.warning("post_write_links failed for %s: %s", rel_path, e)
    try:
        from .config import vault_health_cache
        vault_health_cache().unlink(missing_ok=True)
    except Exception:
        pass


def create_note(vault, folder: str, title: str, content: str,
                ai_generated: bool = True) -> dict:
    """Write a new dated note into `folder` and index it.

    Filename is `{date}-{safe title}.md`, disambiguated by unique_note_path so a
    second note with the same title on the same day cannot overwrite the first —
    that collision used to destroy both the file and its index row.
    """
    cfg = vault.cfg
    resolved_folder = resolve_vault_folder(cfg, folder)
    if not resolved_folder:
        return {"error": f"Invalid folder '{folder}'. Valid: {cfg.vault_folders}"}
    folder = resolved_folder
    if not (title or "").strip():
        return {"error": "title is required"}

    today = datetime.now().strftime("%Y-%m-%d")
    dest = cfg.vault / folder / f"{today}-{safe_filename(title)}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest = unique_note_path(dest)
    # compose_note rather than concatenation: callers routinely include their own
    # frontmatter block, which stacked a second one under the generated block and
    # silently turned every key they wrote into body text.
    full = compose_note(title, content, today, ai_generated=ai_generated)
    try:
        dest.write_text(full, encoding="utf-8")
    except OSError as e:
        return {"error": f"Write failed: {e}"}

    rel = dest.relative_to(cfg.vault).as_posix()

    # Carimbar aqui, e nao dentro do `index_note`. Medido em 26/09/2026:
    # carimbar uma nota custa ~5 ms, desprezivel para uma escrita avulsa, mas
    # o `reindex_vault` tambem passa pelo `index_note`, e la seriam 10.224
    # leituras e escritas do mesmo arquivo, cerca de um minuto so de
    # serializacao. O reindex ja carimba em lote no fim.
    #
    # Sem isto, a nota recem-escrita fica sem carimbo em `.chroma_index.json` e
    # o proximo reindex incremental a reembute inteira, mesmo intocada. E o
    # defeito que o `graphbridge` ja documentava para os artigos de grafo, e
    # que ninguem tinha aplicado ao caminho de escrita de nota: medido, 12
    # notas deste vault estavam nesse estado, todas escritas nesta noite.
    #
    # So em sucesso: `index_note` devolve False quando as linhas nao entraram,
    # e carimbar assim mesmo faria todo reindex seguinte PULAR a nota que
    # acabou de se perder.
    if vault.index_note(full, {"title": title, "path": rel, "folder": folder}):
        vault.stamp_indexed([rel])
    post_write_links(vault, dest, rel, folder, dest.stem)
    return {"status": "ok", "path": rel, "folder": folder, "name": dest.name}


def save_note(vault, rel_path: str, content: str) -> dict:
    """Overwrite an existing note's full text and reindex it.

    Unlike create_note this does not run compose_note: the caller is editing raw
    file content, frontmatter included, so re-composing would inject a second
    generated block on every save.
    """
    cfg = vault.cfg
    dest = resolve_in_vault(cfg.vault, rel_path)
    if dest is None:
        return {"error": f"Path outside the vault: {rel_path}"}
    if not dest.is_file():
        return {"error": f"Not a note: {rel_path}"}
    if dest.suffix != ".md":
        return {"error": "Only .md notes can be saved"}

    try:
        dest.write_text(content, encoding="utf-8")
    except OSError as e:
        return {"error": f"Write failed: {e}"}

    rel = dest.relative_to(cfg.vault).as_posix()
    folder = rel.split("/")[0]
    if vault.index_note(content, {"title": dest.stem, "path": rel, "folder": folder}):
        vault.stamp_indexed([rel])
    try:
        from .config import vault_health_cache
        vault_health_cache().unlink(missing_ok=True)
    except Exception:
        pass
    # Deliberately no post_write_links here: appending a "## Related" block to
    # text the user just typed would edit their note behind them on every save.
    return {"status": "ok", "path": rel, "bytes": len(content.encode("utf-8"))}


# `[[target]]`, `[[target#section]]`, `[[target|display]]`, `[[target#s|d]]`.
# 71 links in this vault carry a section or display part, so a rewriter that
# replaced whole links would silently discard them.
_LINK_RE = re.compile(r"\[\[([^\]|#]+)((?:#[^\]|]+)?(?:\|[^\]]+)?)\]\]")


def _retarget(content: str, old_stem: str, new_stem: str) -> str:
    """Point every `[[old_stem…]]` at new_stem, keeping section and display parts."""
    target = old_stem.strip().lower()

    def sub(m):
        # Both sides stripped: safe_filename used to leave a trailing space on a
        # truncated stem, so five notes in this vault are named "…for the .md"
        # and 85 links spell the target with that space. Comparing a stripped
        # link target against an unstripped stem matched none of them.
        if m.group(1).strip().lower() != target:
            return m.group(0)
        return f"[[{new_stem}{m.group(2)}]]"
    return _LINK_RE.sub(sub, content)


def rename_note(vault, rel_path: str, new_title: str, retitle: bool = True) -> dict:
    """Rename a note and repoint every wikilink that referenced it.

    Renaming without this is silent corruption: a stem is a note's link
    identity, so moving it breaks every `[[stem]]` aimed at it and nothing
    reports the break until someone clicks. It happened during this branch's own
    work — a note renamed by hand left two links dangling — and the blast radius
    here reaches 33 files for the most-referenced note.

    Writes are staged and applied together; if any write fails the files already
    written are restored, so a half-renamed vault is not a reachable state.
    """
    cfg = vault.cfg
    src = resolve_in_vault(cfg.vault, rel_path)
    if src is None:
        return {"error": f"Path outside the vault: {rel_path}"}
    if not src.is_file() or src.suffix != ".md":
        return {"error": f"Not a note: {rel_path}"}
    new_title = (new_title or "").strip()
    if not new_title:
        return {"error": "new_title is required"}

    old_stem = src.stem
    # Keep a leading YYYY-MM-DD-: the note's creation date does not change
    # because its title did.
    date_prefix = ""
    m = re.match(r"^(\d{4}-\d{2}-\d{2}-)(.*)$", old_stem)
    if m:
        date_prefix = m.group(1)
    new_stem = f"{date_prefix}{safe_filename(new_title)}"
    if new_stem == old_stem:
        return {"error": "New title resolves to the same filename"}
    dest = src.with_name(f"{new_stem}.md")
    if dest.exists():
        return {"error": f"A note named {dest.name} already exists"}

    # Stage every file this touches before writing any of them.
    staged: list[tuple[Path, str, str]] = []          # (path, before, after)
    try:
        own = src.read_text(encoding="utf-8")
    except OSError as e:
        return {"error": f"Could not read {rel_path}: {e}"}
    # The frontmatter title is what readers see; leaving the old one normally
    # shows a note whose displayed name disagrees with its filename. retitle=False
    # is for repairing a filename alone — a stem carrying a trailing space from
    # the old safe_filename must not drag the note's real title down to the
    # truncated form the filename happens to hold.
    own_new = own
    if retitle:
        own_new = re.sub(r'^title:.*$', f"title: {yaml_quote_scalar(new_title)}",
                         own, count=1, flags=re.MULTILINE)
    staged.append((src, own, own_new))

    referrers = []
    for folder in cfg.vault_folders:
        root = cfg.vault / folder
        if not root.exists():
            continue
        for f in root.rglob("*.md"):
            if f == src:
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                continue
            retargeted = _retarget(text, old_stem, new_stem)
            if retargeted != text:
                staged.append((f, text, retargeted))
                referrers.append(f.relative_to(cfg.vault).as_posix())

    written: list[tuple[Path, str]] = []
    try:
        for path, before, after in staged:
            path.write_text(after, encoding="utf-8")
            written.append((path, before))
        src.rename(dest)
    except OSError as e:
        for path, before in written:
            try:
                path.write_text(before, encoding="utf-8")
            except OSError:
                logger.error("Rollback failed for %s — vault may be inconsistent", path)
        return {"error": f"Rename failed, changes rolled back: {e}"}

    new_rel = dest.relative_to(cfg.vault).as_posix()
    folder = new_rel.split("/")[0]
    vault.delete_notes([rel_path])
    # Carimbar o que indexou, e so o que indexou. Sem isto um rename deixa a
    # nota renomeada E toda referente sem carimbo, e o proximo reindex
    # "incremental" as reembute inteiras mesmo intocadas.
    #
    # Medido em 26/09/2026, e foi a propria operacao que revelou o defeito: um
    # lote de 75 renomeacoes deixou EXATAMENTE 75 notas sem carimbo, e o
    # reindex seguinte passou minutos reembutindo-as com o encoder na CPU.
    # Este era o sexto caminho de escrita a indexar sem carimbar.
    carimbar: list[str] = []
    if vault.index_note(dest.read_text(encoding="utf-8"),
                        {"title": new_title, "path": new_rel, "folder": folder}):
        carimbar.append(new_rel)
    for path, _, after in staged[1:]:
        rel = path.relative_to(cfg.vault).as_posix()
        if vault.index_note(after, {"title": path.stem, "path": rel,
                                    "folder": rel.split("/")[0]}):
            carimbar.append(rel)
    # Em lote, numa escrita so do arquivo de estado: um rename toca ate dezenas
    # de notas, e carimbar uma a uma reescreveria o arquivo inteiro a cada uma.
    if carimbar:
        vault.stamp_indexed(carimbar)
    try:
        from .config import vault_health_cache
        vault_health_cache().unlink(missing_ok=True)
    except Exception:
        pass

    return {"status": "ok", "path": new_rel, "previous_path": rel_path,
            "links_rewritten": len(referrers), "referrers": referrers}
