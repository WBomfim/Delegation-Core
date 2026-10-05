"""
session.py — Session export: write a curated digest to the vault sessions/ folder.

Called by the export_session MCP tool when the user signals end-of-session.
"""

import logging
from datetime import datetime

logger = logging.getLogger("session")


def export(vault, title: str, summary: str, key_decisions: str = "") -> dict:
    """Write a formatted session note to vault sessions/ and index it immediately.

    vault: VaultManager instance
    title: short descriptive name for the session
    summary: 2-4 sentences covering what was discussed, decided, or built
    key_decisions: comma-separated decisions, artifacts created, or next steps
    """
    cfg = vault.cfg
    # Case-insensitive: a vault with a "Sessions" folder must not fall through to
    # vault_folders[0], which silently filed every session digest under Projects/.
    from .config import resolve_folder
    folder = resolve_folder("sessions", cfg.vault_folders) or cfg.vault_folders[0]

    from .vault import compose_note, safe_filename, unique_note_path
    safe = safe_filename(title)
    date_str = datetime.now().strftime("%Y-%m-%d")
    dest = cfg.vault / folder / f"{date_str}-{safe}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest = unique_note_path(dest)

    decisions_list = [d.strip() for d in key_decisions.split(",") if d.strip()] if key_decisions else []

    lines = [
        f"# {title}",
        f"*{datetime.now().strftime('%Y-%m-%d %H:%M')}*",
        "",
        "## Summary",
        "",
        summary.strip(),
        "",
    ]
    if decisions_list:
        lines += ["## Key decisions / artifacts", ""]
        for d in decisions_list:
            lines.append(f"- {d}")
        lines.append("")

    # compose_note, e nao um bloco de frontmatter montado a mao aqui.
    #
    # safe_filename corta o nome do arquivo em 50 caracteres, e compose_note
    # responde a isso gravando o titulo inteiro em `aliases:`, para que um link
    # escrito com o titulo real resolva. Montando o bloco a mao, export_session
    # nunca recebia esse alias.
    #
    # Medido neste vault em 03/09/2026: DEZ notas de Sessions tem titulo maior
    # que o proprio nome de arquivo e NENHUMA tem alias. A maior delas tem 95
    # caracteres de titulo contra 49 de stem. Todo wikilink escrito com o titulo
    # real dessas notas resolve para nada, e export_session e justamente a
    # ferramenta que o AGENT_GUIDE manda chamar ao fim de toda sessao.
    #
    # `type: session` vai no bloco do chamador porque compose_note preserva o
    # que o autor fornece e so acrescenta o que falta.
    full = compose_note(title, "---\ntype: session\n---\n\n" + "\n".join(lines), date_str)
    dest.write_text(full, encoding="utf-8")
    rel = dest.relative_to(cfg.vault).as_posix()
    # Carimba so em sucesso: ver a nota em notewriter.create_note. Sem isto, o
    # digest de sessao que acabou de ser escrito e reembutido inteiro no
    # proximo reindex incremental, mesmo intocado.
    if vault.index_note(
        full,
        {"title": title, "path": rel, "folder": folder, "type": "session"},
    ):
        vault.stamp_indexed([rel])
    logger.info("Session exported: %s", dest.name)
    return {"status": "ok", "path": str(dest.name), "folder": folder}
