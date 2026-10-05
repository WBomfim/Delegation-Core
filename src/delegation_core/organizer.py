"""
organizer.py — Inbox maintenance pipeline orchestrator.

Pipeline per inbox file:
  1. extract    — extractor.extract()
  2. filter     — junk.is_junk()
  3. sidecar    — sidecar.load()
  4. route      — sidecar folder_hint, else classifier.classify()
  5. SPLIT?     — splitter.should_split() (v0.3): if file is large or multi-page PDF,
                  split into N sections and process each through steps 6–7 independently,
                  then cross-link all sibling notes via splitter.inject_sibling_links()
  6. synthesize — synthesizer.synthesize() (skipped if cfg.synthesis_enabled=False)
  7. merge/file — merger.try_merge(), else write new note + linker.wikilinks()
  8. move       — source + sidecar → _processed/

relink_folder is re-exported here for server.py convenience.
"""

import asyncio
import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

from .classifier import classify
from .extractor import SUPPORTED, extract, format_label, is_no_text_stub
from .junk import is_junk
from .linker import (  # noqa: F401 — some re-exported
    clean_display, ensure_aliases, inject_backlinks, relink_folder, wikilinks,
)
from .merger import try_merge
from .config import resolve_folder
from .sidecar import is_sidecar, load as load_sidecar, resolve_folder_hint, sidecar_for
from .splitter import inject_sibling_links, should_split
from .synthesizer import synthesize
from .vault import safe_filename, unique_note_path, yaml_quote_scalar, yaml_unquote_scalar

from .config import lang_instruction as _frase_de_idioma, with_lang as _com_idioma

#: Onde vai a nota-toco quando o arquivo nao traz pista de pasta.
#: `Reference` porque o toco e um registro de que o arquivo existe, nao uma
#: decisao, um conserto nem uma sessao.
_PASTA_DE_TOCOS = "Reference"

logger = logging.getLogger("organizer")

# ── merge opt-out (ported forward from the 0.1.0 hardening of field deployment A) ───────────────
# The v5.1 refactor kept automatic size-based merge suppression but dropped the
# ability for a specific source to forbid being merged into an existing note.
# Restored here, honoring both mechanisms:
#   • sidecar key   no_merge: true       (v5.1-native .meta.yaml routing)
#   • inline YAML    vault_merge: false   (original 0.1.0 frontmatter syntax)
_VAULT_MERGE_RE = re.compile(r"^vault_merge:\s*(\S+)\s*$", re.MULTILINE)


def _merge_forbidden(sc: dict | None, raw_text: str) -> bool:
    """True if this source explicitly opts out of merging into an existing note."""
    if sc and str(sc.get("no_merge", "")).strip().lower() in ("true", "yes", "on", "1"):
        return True
    if raw_text.startswith("---\n"):
        end = raw_text.find("\n---\n", 4)
        if end != -1:
            m = _VAULT_MERGE_RE.search(raw_text[4:end])
            if m and m.group(1).strip().strip('"').strip("'").lower() in ("false", "no", "off", "0"):
                return True
    return False


_HALLUCINATION_NAMES = frozenset({
    "john doe", "jane doe", "maria oliveira", "paulo pereira",
    "josé da silva", "ana santos", "sarah chen", "carlos souza", "voodoo",
})

_PROMPT_LEAK_PATTERNS = [
    r"(?i)write a (structured )?note",
    r"(?i)you are an? ai",
    r"(?i)as an ai",
    r"(?i)summarize the following",
    r"(?i)extract key (facts|information)",
    r"(?i)no preamble",
    r"(?i)vault (analyst|reporter|synthesizer)",
]

# Tier 3: placeholder title detection — matches auto-generated section names that
# should be upgraded to descriptive titles before synthesis.
_PLACEHOLDER_RE = re.compile(
    r"""^(
        Section \s+ \d+ (?: \s* [–\-] \s* \d+ )?   |
        Pages?  \s+ \d+ (?: \s* [–\-] \s* \d+ )?   |
        Part    \s+ \d+                              |
        \d+     (?: \s* [–\-] \s* \d+ )?
    )$""",
    re.VERBOSE | re.IGNORECASE,
)


def _is_placeholder(title: str) -> bool:
    return bool(_PLACEHOLDER_RE.match(title.strip()))


async def _upgrade_section_titles(
    engine,
    sections: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Tier 3: replace placeholder section titles with LLM-generated descriptive titles.

    Real H1/H2 heading text is never rewritten. Runs all upgrades concurrently via
    asyncio.gather() so cost is one round-trip regardless of section count.
    """
    async def _upgrade_one(title: str, content: str) -> str:
        if not _is_placeholder(title):
            return title
        return await _titulo_de_secao(engine, title, content)

    upgraded = await asyncio.gather(
        *(_upgrade_one(t, c) for t, c in sections)
    )
    return [(new_t, orig[1]) for new_t, orig in zip(upgraded, sections)]


def _synthesis_quality(output: str, raw_input: str) -> tuple[float, list]:
    """Score synthesis output 0.0–1.0. Returns (score, [issue_strings])."""
    issues = []
    stripped = output.strip()

    if len(stripped) < 30:
        return 0.0, ["output_too_short"]

    # Only check compression ratio for substantial inputs; short content legitimately expands
    if len(raw_input) > 500:
        ratio = len(stripped) / max(len(raw_input), 1)
        if ratio > 0.85:
            issues.append("compression_failed")

    lower = stripped.lower()
    for name in _HALLUCINATION_NAMES:
        if name in lower:
            issues.append(f"hallucinated_name:{name}")

    for pattern in _PROMPT_LEAK_PATTERNS:
        if re.search(pattern, stripped):
            issues.append("prompt_leak")
            break

    score = max(0.0, round(1.0 - 0.3 * len(issues), 2))
    return score, issues


def _inject_quality_frontmatter(
    content: str, score: float, issues: list, needs_review: bool
) -> str:
    """Inject or update quality_score/quality_issues/needs_review in YAML frontmatter."""
    quality_yaml = (
        f"quality_score: {score}\n"
        f"quality_issues: {json.dumps(issues)}\n"
        f"needs_review: {str(needs_review).lower()}"
    )

    if content.startswith("---\n"):
        close = content.find("\n---\n", 4)
        if close != -1:
            fm_text = content[4:close]
            clean_lines = [
                line for line in fm_text.splitlines()
                if not line.startswith(("quality_score:", "quality_issues:", "needs_review:"))
            ]
            fm_body = "\n".join(clean_lines)
            if fm_body and not fm_body.endswith("\n"):
                fm_body += "\n"
            return f"---\n{fm_body}{quality_yaml}\n---\n{content[close + 5:]}"

    return f"---\n{quality_yaml}\n---\n\n{content}"


async def heal(engine, vault_manager) -> dict:
    """Re-synthesize low-quality notes. Bounded by cfg.heal_per_run per call.

    Reads notes with needs_review: true or quality_score below threshold,
    re-synthesizes the body, and overwrites only if the new score improves.
    """
    cfg = vault_manager.cfg
    all_candidates = vault_manager.get_notes_needing_repair()
    total_needing = len(all_candidates)
    candidates = all_candidates[:cfg.heal_per_run]

    healed = failed = 0
    today_str = datetime.now().strftime("%Y-%m-%d")

    for note in candidates:
        note_path = cfg.vault / note["path"]
        content = note["content"]

        # Preserve original title/date before stripping frontmatter
        orig_fm: dict = {}
        body = content
        if content.startswith("---\n"):
            close = content.find("\n---\n", 4)
            if close != -1:
                for line in content[4:close].splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        if k.strip() in ("title", "date", "ai_generated"):
                            orig_fm[k.strip()] = yaml_unquote_scalar(v)
                body = content[close + 5:]

        related_match = re.search(r"\n## Related\b", body)
        if related_match:
            body = body[:related_match.start()]
        body = body.strip()

        if len(body) < 50:
            failed += 1
            continue

        try:
            folder = note["path"].split("/")[0]
            note_stem = Path(note["path"]).stem
            title_hint = orig_fm.get("title") or note_stem
            synthesized = await synthesize(engine, {}, body, title_hint, "md", today_str)
            q_score, q_issues = _synthesis_quality(synthesized, body)

            if q_score >= cfg.quality_threshold:
                healed_content = _inject_quality_frontmatter(
                    synthesized, q_score, q_issues, needs_review=False
                )
                # Restore original title/date if the synthesizer didn't produce frontmatter
                if orig_fm and healed_content.startswith("---\n"):
                    close = healed_content.find("\n---\n", 4)
                    if close != -1:
                        fm_text = healed_content[4:close]
                        for key in ("title", "date", "ai_generated"):
                            if key in orig_fm and f"{key}:" not in fm_text:
                                value = (
                                    yaml_quote_scalar(orig_fm[key])
                                    if key == "title"
                                    else orig_fm[key]
                                )
                                fm_text = f"{key}: {value}\n" + fm_text
                        healed_content = f"---\n{fm_text}\n---\n{healed_content[close + 5:]}"

                # Re-inject wikilinks using search_threshold (not merge_threshold)
                hits = vault_manager.search(healed_content[:600], limit=5)
                links = wikilinks(hits, cfg.search_threshold, cfg.vault)
                if links:
                    healed_content = healed_content.rstrip() + f"\n\n## Related\n{links}\n"
                    linked_paths = [h["path"] for h in hits
                                    if h.get("similarity", 0) >= cfg.search_threshold]
                    inject_backlinks(vault_manager, note_stem, linked_paths)

                note_path.write_text(healed_content, encoding="utf-8")
                vault_manager.index_note(
                    healed_content,
                    {"title": orig_fm.get("title") or note_stem, "path": note["path"], "folder": folder},
                )
                healed += 1
                logger.info("Healed %s (score: %.2f)", note["path"], q_score)
            else:
                failed += 1
        except Exception as e:
            logger.warning("Heal failed for %s: %s", note["path"], e)
            failed += 1

    if healed > 0:
        from .config import vault_health_cache
        cache_path = vault_health_cache()
        try:
            cache_path.unlink(missing_ok=True)
        except Exception:
            pass

    return {
        "healed": healed,
        "failed": failed,
        "remaining": total_needing - len(candidates),
    }


async def run(engine, vault_manager) -> dict:
    cfg = vault_manager.cfg
    inbox:     Path = cfg.vault / "_inbox"
    processed: Path = cfg.vault / "_processed"
    failed:    Path = cfg.vault / "_failed"

    for d in (inbox, processed, failed):
        d.mkdir(parents=True, exist_ok=True)

    all_files     = [f for f in inbox.iterdir() if f.is_file()]
    sidecar_files = [f for f in all_files if is_sidecar(f)]
    inbox_files   = [f for f in all_files if not is_sidecar(f) and f.suffix.lower() in SUPPORTED]
    skipped       = [f.name for f in all_files if not is_sidecar(f) and f.suffix.lower() not in SUPPORTED]

    results: dict = {
        "classified": [], "merged": [], "linked": [], "errors": [],
        "skipped": skipped, "junk": [], "sidecars_archived": [],
    }

    if not inbox_files:
        _archive_sidecars(sidecar_files, processed, results)
        results["message"] = "Inbox empty — nothing to process."
        return results

    today_str = datetime.now().strftime("%Y-%m-%d")

    for f in inbox_files:
        try:
            raw_text = extract(f)
            # .strip(), not just falsiness: extract() returns the file's bytes
            # verbatim for text formats, so a file of nothing but blank lines is
            # a truthy "\n\n\n" and sailed past a bare `if not raw_text`. It then
            # reached classify() — an LLM handed blank text picks an arbitrary
            # folder — and synthesize(), writing an empty note into the vault.
            # ingest.py has always checked .strip() here; this path had not.
            if not raw_text or not raw_text.strip():
                results["errors"].append(f"{f.name}: extraction returned no text (scanned or protected?)")
                shutil.move(str(f), str(failed / f.name))
                continue

            # Um toco de extracao vazia e texto VALIDO: nao-vazio, com mais de
            # 30 caracteres, e portanto passa pela guarda acima, por `classify()`
            # e por `synthesize()` sem que nada o barre. O avaliador de sintese
            # tambem nao ajuda: ele so reprova saida curta demais e nomes
            # alucinados conhecidos, entao uma nota inteira derivada de
            # "[no extractable text] File: deck.pptx" sai com quality_score 1.0
            # e cara de registro de verdade.
            #
            # Foi assim que 22 notas com resumo, topicos, pessoas e decisoes
            # foram sintetizadas a partir de decks cujos slides eram imagens
            # renderizadas. O modelo nao errou ao resumir: recebeu nada e
            # preencheu o formulario.
            #
            # O toco vira o corpo da nota literalmente, sem passar pelo modelo.
            # O arquivo continua achavel pelo nome, que e o ponto de existir uma
            # nota, e nao existe uma frase inventada dentro dela.
            if is_no_text_stub(raw_text):
                folder = resolve_folder_hint(
                    (sc_toco := load_sidecar(f)) and sc_toco.get("folder_hint"),
                    cfg.vault_folders,
                ) or _PASTA_DE_TOCOS
                # Escrito pelo mesmo caminho do fluxo normal, logo abaixo:
                # `VaultManager` nao tem `write_note`, e a primeira versao disto
                # chamava um metodo que nao existe. A suite nao pegou porque o
                # dublê do teste tinha o metodo que eu tinha suposto, em vez da
                # superficie da classe de verdade. Quem pegou foi um deck real
                # passando pelo daemon no ar.
                safe = safe_filename(f.stem)
                caminho = cfg.vault / folder / f"{today_str}-{safe}.md"
                caminho.parent.mkdir(parents=True, exist_ok=True)
                caminho = unique_note_path(caminho)
                corpo = ensure_aliases(
                    raw_text, [clean_display(f"{today_str}-{safe}"), f.stem])
                caminho.write_text(corpo, encoding="utf-8")
                rel_toco = caminho.relative_to(cfg.vault).as_posix()
                vault_manager.index_note(
                    corpo, {"title": f.stem, "path": rel_toco, "folder": folder})
                results.setdefault("stubs", []).append(
                    f"{f.name} → {rel_toco} (no text layer, no synthesis)"
                )
                shutil.move(str(f), str(processed / f.name))
                _archive_sidecar(f, processed, results)
                logger.info("No-text stub for %s → %s", f.name, caminho)
                continue

            junk_reason = is_junk(f.name, raw_text)
            if junk_reason:
                results["junk"].append(f"{f.name}: skipped — {junk_reason}")
                shutil.move(str(f), str(processed / f.name))
                continue

            sc  = load_sidecar(f)
            fmt = format_label(f)
            no_merge = _merge_forbidden(sc, raw_text)

            hint = sc.get("folder_hint") if sc else None
            # A pasta ja vem com a caixa do vault: usar o hint cru criaria
            # `sessions/` ao lado da `Sessions/` existente.
            roteado = resolve_folder_hint(hint, cfg.vault_folders)
            if roteado:
                folder = roteado
                logger.info("Routing %s → %s/ (sidecar)", f.name, folder)
            else:
                folder = await classify(engine, cfg.vault_folders, f.name, raw_text, fmt)

            # ── Recursive split path (v0.3) ────────────────────────────────
            sections = should_split(raw_text, f, cfg)
            if sections:
                created_paths = await _process_sections(
                    engine, vault_manager, cfg,
                    sections, sc, fmt, folder, f, today_str, results, no_merge,
                )
                if len(created_paths) > 1:
                    n = inject_sibling_links(vault_manager, created_paths, cfg)
                    if n:
                        results.setdefault("linked_recursive", []).append(
                            f"{f.name}: {n} notes cross-linked"
                        )
                shutil.move(str(f), str(processed / f.name))
                _archive_sidecar(f, processed, results)
                continue
            # ── end recursive split path ───────────────────────────────────

            # Synthesis: convert raw text → structured note (skippable via config)
            if cfg.synthesis_enabled:
                note_content = await synthesize(engine, sc, raw_text, f.name, fmt, today_str)
                q_score, q_issues = _synthesis_quality(note_content, raw_text)
                if q_score < cfg.quality_threshold:
                    note_content = raw_text
                    q_issues.append("wrote_raw_fallback")
                needs_review = q_score < cfg.quality_threshold
                note_content = _inject_quality_frontmatter(note_content, q_score, q_issues, needs_review)
            else:
                note_content = raw_text

            hits = vault_manager.search(note_content[:600], limit=5)

            if no_merge:
                merged, merged_path = False, None
            else:
                merged, merged_path = try_merge(vault_manager, hits, raw_text, note_content, folder, f.name, today_str)

            if merged:
                results["merged"].append(f"{f.name} → {merged_path}")
            else:
                links = wikilinks(hits, cfg.merge_threshold, cfg.vault)
                final = note_content + (f"\n\n## Related\n{links}" if links else "")

                safe = safe_filename(f.stem)
                # v6: register the readable title as an Obsidian alias so `[[Title]]`
                # links resolve even though the file is named `YYYY-MM-DD-slug.md`.
                final = ensure_aliases(final, [clean_display(f"{today_str}-{safe}"), f.stem])
                dest = cfg.vault / folder / f"{today_str}-{safe}.md"
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest = unique_note_path(dest)
                dest.write_text(final, encoding="utf-8")
                rel = dest.relative_to(cfg.vault).as_posix()
                vault_manager.index_note(final, {"title": f.stem, "path": rel, "folder": folder})
                results["classified"].append(f"{f.name} → {rel}")
                if links:
                    results["linked"].append(dest.name)
                    linked_paths = [h["path"] for h in hits
                                    if h.get("similarity", 0) >= cfg.merge_threshold]
                    inject_backlinks(vault_manager, dest.stem, linked_paths)

            shutil.move(str(f), str(processed / f.name))

            _archive_sidecar(f, processed, results)

        except Exception as e:
            logger.error("Failed %s: %s", f.name, e)
            results["errors"].append(f"{f.name}: {e}")
            try:
                shutil.move(str(f), str(failed / f.name))
            except Exception:
                pass

    _archive_sidecars(sidecar_files, processed, results)
    await _write_summary(engine, cfg, results)
    return results


def _archive_sidecar(src: Path, processed: Path, results: dict):
    """Move the .meta.yaml sidecar (if any) for src into processed/."""
    sc_path = sidecar_for(processed / src.name) or sidecar_for(src)
    if sc_path and sc_path.exists():
        try:
            shutil.move(str(sc_path), str(processed / sc_path.name))
            results["sidecars_archived"].append(sc_path.name)
        except Exception as e:
            logger.warning("Could not archive sidecar %s: %s", sc_path.name, e)


async def _process_sections(
    engine, vault_manager, cfg,
    sections: list[tuple[str, str]],
    sc: dict | None,
    fmt: str,
    folder: str,
    src: Path,
    today_str: str,
    results: dict,
    no_merge: bool = False,
) -> list[str]:
    """Synthesize, merge-or-file each section from a recursive split.

    Returns vault-relative paths of all newly created notes (not merged ones).
    Merged sections are recorded in results["merged"] as usual.
    """
    created_paths: list[str] = []

    # Tier 3: upgrade placeholder titles before synthesis so synthesize() gets
    # a meaningful label hint instead of a bare "Section N" or "Page N–N".
    sections = await _upgrade_section_titles(engine, sections)

    for title, section_text in sections:
        try:
            label = f"{src.stem} — {title}"

            if cfg.synthesis_enabled:
                note_content = await synthesize(engine, sc, section_text, label, fmt, today_str)
                q_score, q_issues = _synthesis_quality(note_content, section_text)
                if q_score < cfg.quality_threshold:
                    note_content = section_text
                    q_issues.append("wrote_raw_fallback")
                needs_review = q_score < cfg.quality_threshold
                note_content = _inject_quality_frontmatter(note_content, q_score, q_issues, needs_review)
            else:
                note_content = section_text

            hits = vault_manager.search(note_content[:600], limit=5)
            if no_merge:
                merged, merged_path = False, None
            else:
                merged, merged_path = try_merge(
                    vault_manager, hits, section_text, note_content, folder, label, today_str
                )

            if merged:
                results["merged"].append(f"{src.name}[{title}] → {merged_path}")
            else:
                links = wikilinks(hits, cfg.merge_threshold, cfg.vault)
                final = note_content + (f"\n\n## Related\n{links}" if links else "")
                safe  = safe_filename(f"{src.stem}–{title}")
                final = ensure_aliases(final, [clean_display(f"{today_str}-{safe}"), label])
                dest  = cfg.vault / folder / f"{today_str}-{safe}.md"
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest = unique_note_path(dest)
                dest.write_text(final, encoding="utf-8")
                rel = dest.relative_to(cfg.vault).as_posix()
                vault_manager.index_note(final, {"title": title, "path": rel, "folder": folder})
                results["classified"].append(f"{src.name}[{title}] → {rel}")
                if links:
                    results["linked"].append(dest.name)
                    linked_paths = [h["path"] for h in hits
                                    if h.get("similarity", 0) >= cfg.merge_threshold]
                    inject_backlinks(vault_manager, dest.stem, linked_paths)
                created_paths.append(rel)

        except Exception as e:
            logger.error("Section '%s' from %s failed: %s", title, src.name, e)
            results["errors"].append(f"{src.name}[{title}]: {e}")

    return created_paths


def _archive_sidecars(sidecar_files: list[Path], processed: Path, results: dict):
    for sc in sidecar_files:
        if sc.exists():
            try:
                shutil.move(str(sc), str(processed / sc.name))
                results["sidecars_archived"].append(sc.name)
            except Exception as e:
                logger.warning("Could not archive orphan sidecar %s: %s", sc.name, e)


async def _titulo_de_secao(engine, title: str, content: str) -> str:
    """Um titulo de 3 a 5 palavras para uma secao cujo titulo e so um marcador.

    A ordem do prompt e invertida de proposito em relacao aos outros: o conteudo
    vem primeiro porque e ele que o modelo precisa ler. Isso tornava a heuristica
    do fallback de modo agente ainda pior aqui -- ela devolveria a INSTRUCAO --
    entao a carga vai declarada, e o que sai quando nao ha modelo e o titulo que
    ja estava la.

    Manter o marcador e a resposta honesta: inventar um titulo sem ter lido o
    conteudo, ou por o proprio prompt como CABECALHO da nota, sao os dois piores
    desfechos possiveis para uma funcao que escreve no vault do usuario.
    """
    prompt = (
        f"Content summary: {content[:200]}\n\n"
        "Write a 3-5 word title for this section. Title only, no punctuation:"
        + (f"\n{_frase_de_idioma(engine.cfg)}" if _frase_de_idioma(engine.cfg) else "")
    )
    try:
        result = await engine.invoke(
            prompt,
            task="section_title",
            max_tokens=0,
            temperature=0.0,
            fallback_payload=title,
        )
        new_title = result.strip()
        if not new_title or len(new_title) > 120:
            return title
        return new_title
    except Exception as exc:
        logger.warning("Title upgrade failed for '%s': %s - keeping original", title, exc)
        return title


def _resumo_deterministico(results: dict) -> str:
    """O resumo que os proprios numeros ja dao, sem modelo nenhum.

    Serve de `fallback_payload` no modo agente e de rede quando a chamada falha.
    Antes, o modo agente devolvia o PROMPT, e ele foi para o vault: o
    `2026-W35-maintenance.md` desta maquina comeca com "Write a 3-sentence vault
    maintenance summary." seguido dos dados crus.
    """
    n = lambda k: len(results.get(k, []))
    partes = [f"{n('classified')} nota(s) classificada(s)",
              f"{n('merged')} fundida(s)"]
    if n("junk"):
        partes.append(f"{n('junk')} descartada(s) como boilerplate")
    if n("errors"):
        partes.append(f"{n('errors')} com erro")
    return "Passe de manutencao: " + ", ".join(partes) + "."


async def _resumo_de_manutencao(engine, results: dict) -> str:
    """O corpo do resumo semanal. Separado de `_write_summary` para ser testavel
    sem tocar disco."""
    classified = "\n".join(results.get("classified", [])) or "none"
    merged     = "\n".join(results.get("merged", []))     or "none"
    errors     = "\n".join(results.get("errors", []))     or "none"
    junk       = "\n".join(results.get("junk", []))       or "none"
    deterministico = _resumo_deterministico(results)

    try:
        return await engine.invoke(
            f"Write a 3-sentence vault maintenance summary.\n\n"
            f"Classified: {classified}\nMerged: {merged}\nErrors: {errors}\n"
            f"Skipped as junk: {junk}",
            system=_com_idioma("Vault Reporter", engine.cfg),
            max_tokens=engine.budget("summary", 200),
            temperature=0.3,
            task="summary",
            fallback_payload=deterministico,
        )
    except Exception as e:
        logger.warning("Maintenance summary failed: %s - using the counted one", e)
        return deterministico


async def _write_summary(engine, cfg, results: dict):
    """Append a dated maintenance section to the weekly summary note."""
    week     = datetime.now().strftime("%Y-W%W")
    # resolve_folder, e nao o literal minusculo: a pasta deste vault chama-se
    # "Sessions", e num sistema de arquivos sensivel a caixa `cfg.vault /
    # "sessions"` NAO EXISTE. O resumo semanal caia na RAIZ do vault, que esta
    # fora de toda pasta configurada, entao ele nunca era indexado (conferido:
    # nenhuma chave sem barra em .chroma_index.json), nunca era graduado pela
    # saude, e nao aparecia em search_vault. A prova estava no disco:
    # `<vault>/2026-W35-maintenance.md`.
    #
    # E o mesmo defeito que `merger._never_merge` documenta sobre si mesmo e que
    # o docstring de `config.resolve_folder` enumera: "Code that hardcoded a
    # lowercase name and tested `"sessions" in folders` silently did the wrong
    # thing on such a vault".
    pasta_sessoes = resolve_folder("sessions", cfg.vault_folders)
    out_dir = (cfg.vault / pasta_sessoes) if pasta_sessoes else cfg.vault
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / f"{week}-maintenance.md"

    body = await _resumo_de_manutencao(engine, results)

    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    section  = f"\n\n## Run {date_str}\n\n{body}\n"
    existing = summary_path.read_text(encoding="utf-8") if summary_path.exists() else ""
    if existing:
        summary_path.write_text(existing.rstrip() + section, encoding="utf-8")
    else:
        summary_path.write_text(
            f"---\ntitle: Maintenance {week}\ndate: {datetime.now().strftime('%Y-%m-%d')}\n"
            f"tags: [maintenance]\n---\n{section}",
            encoding="utf-8",
        )
