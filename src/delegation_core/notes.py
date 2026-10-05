"""
notes.py — nomes de nota, caminhos e frontmatter. Nada de ChromaDB aqui.

Extraido de vault.py, que tinha 2020 linhas fazendo duas coisas sem relacao:
o ciclo de vida do indice vetorial, e as regras de como uma nota se chama e
como seu frontmatter e montado. As segundas sao funcoes puras, testaveis sem
GPU, sem indice e sem disco, e sao as que o resto do projeto mais importa:
nove modulos pedem `safe_filename`, `yaml_quote_scalar`, `compose_note` ou
`client_from_path` a `vault`, e nenhum deles quer o VaultManager junto.

O corte e onde as dependencias mudam. Tudo aqui depende so da biblioteca
padrao; nada aqui importa chromadb,
embeddings ou gpu. E por isso que o bloco sai inteiro sem tocar em nenhuma
linha do que ficou.

`vault.py` reexporta todos estes nomes, entao `from .vault import
safe_filename` continua funcionando. A reexportacao e deliberada e tem teste:
tira-la quebraria nove modulos e qualquer instalacao de campo que importe
daqui, e o ganho seria zero.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import unicodedata
from pathlib import Path, PurePosixPath

from . import locking

logger = logging.getLogger("notes")

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# v0.12: a vault note is stored as one row per chunk, keyed "<rel path>::chunk_N".
# Anchored at the end because "::" is a legal character in a POSIX filename — an
# unanchored search would mistake a note actually named "notes::chunk_2 draft.md"
# for a chunk of "notes" and, in the orphan sweep, delete it as an orphan of a
# base path that never existed.
_CHUNK_SUFFIX_RE = re.compile(r"::chunk_\d+$")
# The same shape, but only for a *non-zero* chunk: every indexed document has
# exactly one row that this does NOT match (::chunk_0, or a legacy bare path),
# which is what makes counting distinct documents an id-only scan in get_stats().
_EXTRA_CHUNK_SUFFIX_RE = re.compile(r"::chunk_(?!0$)\d+$")

# v6 health de-pollution: `[[...]]` occurs in ingested content that is NOT a
# wikilink — bash `[[ -f "$x" ]]` test syntax, imported Obsidian path-links
# `[[Folder/File.pdf]]`, prose. Counting those made broken_links ~98% false
# positives. These strip code spans and keep only note-like link targets.
# One pass for fenced blocks AND inline spans: a code span opens with a run of N
# backticks and closes with a run of exactly N (CommonMark). The old two-regex
# approach (fences, then inline) desynced on notes that *document* fence syntax —
# a literal ``` inside inline code (`` ` ``` ` ``) left an unpaired fence, which
# shifted inline pairing for the rest of the file and exposed the `[[...]]`
# examples in it as "broken links". Lookarounds pin the run to its exact length so
# ``` never closes against one backtick of a longer run.
_CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(?:.*?)(?<!`)\1(?!`)", re.DOTALL)
_WIKILINK_TARGET_RE = re.compile(r"\[\[([^\]\|#]+)")


def _countable_wikilinks(content: str) -> list[str]:
    """Return note-like wikilink targets from a note, excluding code spans and
    non-link `[[...]]` artifacts (shell test syntax, path/file references).
    Preserves real note targets containing currency signs or symbols (e.g. `R$13,8M`)."""
    body = _CODE_SPAN_RE.sub(" ", content)
    out = []
    for raw in _WIKILINK_TARGET_RE.findall(body):
        t = raw.strip()
        if not t:
            continue
        # Exclude shell test flags: `[[ -f ... ]]`, `[[ ! ... ]]`
        if t.startswith(("-", "!")):
            continue
        # Exclude shell conditional expressions: `==`, `!=`, `=~`, `&&`, `||`, `-eq`, etc.
        if re.search(r"\s+(==|!=|=~|&&|\|\||-eq|-ne|-lt|-gt|-le|-ge)\s+", t):
            continue
        # Exclude pure shell variable references: `$VAR`, `"$VAR"`, `"${VAR}"`
        if re.fullmatch(r'["\']?\$\{?[A-Za-z_][A-Za-z0-9_]*\}?["\']?', t):
            continue
        if "/" in t or re.search(r"\.(pdf|docx?|md|png|jpe?g|xlsx?|pptx?)$", t, re.I):
            continue                         # imported path/file reference, not a note
        out.append(t)
    return out


#: A note's filename begins with the date it was written — write_note builds
#: `{date}-{safe title}.md`. Nobody writes that date when they link to the note:
#: they write the title. So `[[Diagnóstico — notas do vault]]` never resolved
#: against `2026-08-22-Diagnóstico — notas do vault.md`, and the vault
#: accumulated "broken" links to notes that were sitting right there. Three of
#: the first four broken links audited on this vault were this and nothing else.
_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[-_ ]")


def link_names_for_stem(stem: str) -> set[str]:
    """Every name a note with this filename stem can be linked by, lowercased.

    The stem itself, plus the stem with its leading date stripped. Both are
    returned so resolution stays additive — stripping the date never removes a
    name, it only adds the one people actually type.

    One function because the health pass and note_links() each built this set
    themselves, and a set built twice is a set that disagrees with itself: the
    backlinks panel called nine live targets missing while the health count did
    not, which is the exact class of drift this centralises away.
    """
    s = (stem or "").strip().lower()
    if not s:
        return set()
    names = {s}
    stripped = _DATE_PREFIX_RE.sub("", s).strip()
    if stripped:
        names.add(stripped)
    return names


def resolve_vault_folder(cfg, folder: str | None) -> str | None:
    """Resolve a folder name against cfg.vault_folders case-insensitively.

    Vaults configure their own casing (e.g. "Projects" vs "projects"), and
    agents or callers may pass different capitalization or trailing slashes.
    Matches case-insensitively, returning the exact configured string from
    cfg.vault_folders on hit, or None on miss.
    """
    if not folder:
        return None
    target = str(folder).strip().rstrip("/\\").lower()
    folders = getattr(cfg, "vault_folders", None) or []
    for f in folders:
        if str(f).strip().lower() == target:
            return str(f).strip()
    return None



def client_slug(value: str, aliases: dict | None = None) -> str:
    """Normalise a client name to the single form the index is keyed by.

    ChromaDB's `where` is exact equality, so an unnormalised key is a filter
    that silently under-returns: one vault holds `Nortex` on 111 notes and
    `nortex` on 13, and a query for either missed the other's rows without
    saying so. Lowercased, accent-folded, and everything that is not
    alphanumeric collapsed to a single hyphen.

    THE SAME FUNCTION MUST RUN ON BOTH SIDES — promotion and query. A patch
    that normalised only on the way in leaves `client="Nortex"` missing every
    row it just normalised to `nortex`, which looks exactly like "that client
    has no notes".

    Folding cannot merge genuinely different strings: `Campo Incorporadora`
    and `campo` slug apart, and deciding they are one client is a judgement
    about the data, not about text. `aliases` carries those decisions —
    {slug: canonical slug}, from config, empty by default.
    """
    if not value:
        return ""
    folded = unicodedata.normalize("NFKD", str(value))
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    if not slug:
        return ""
    return (aliases or {}).get(slug, slug)


def client_from_path(path: str, roots: list[str] | None = None,
                     aliases: dict | None = None) -> str:
    """Derive a client from an external file's path, or "" when unsure.

    92.8% of the rows in one real index are ingested files, so a client filter
    blind to them reaches 7% of the corpus and reads as "almost nothing matched".
    But a WRONG label is worse than none: an unlabelled document still surfaces
    in an unfiltered search, while a mislabelled one is silently excluded from
    the filter that should have found it.

    So this guesses nothing. `roots` is an explicit, configured list of parent
    directories, and the client is the single path segment directly beneath the
    matching root — `/Work/Acme/Nortex/deck.pdf` under root `/Work/Acme`
    gives `nortex`. No configured root matches, no client. Empty by default, so
    an install that has not opted in cannot be mislabelled by this at all.
    """
    if not path or not roots:
        return ""
    try:
        p = PurePosixPath(path)
    except (TypeError, ValueError):
        return ""
    for root in roots:
        if not root:
            continue
        try:
            rel = p.relative_to(PurePosixPath(root))
        except ValueError:
            continue
        parts = rel.parts
        # Only a file *inside* a client directory counts. A file sitting
        # directly in the root has no client segment to read.
        if len(parts) >= 2:
            return client_slug(parts[0], aliases)
    return ""


def resolve_in_vault(vault_root: Path, rel_path: str) -> Path | None:
    """Resolve *rel_path* under *vault_root*, or None if it escapes.

    Containment is checked with Path.relative_to, never a string prefix: a
    sibling directory whose name merely starts with the vault's own name
    (vault at .../vault, and .../vault-old exists) passes a prefix test while
    resolving outside. That exact bug was fixed twice in this codebase before —
    in relink_folder and in the dashboard's note route — so the check lives in
    one place now rather than being re-typed at each new call site.
    """
    # O `resolve()` fica DENTRO do try. Ele levanta por conta propria para
    # entradas que nao chegam a ser um caminho -- um `\x00` no meio da string da
    # ValueError("embedded null character"), e um componente absurdamente longo
    # da OSError -- e o contrato desta funcao promete None, nao excecao.
    #
    # MEDIDO ponta a ponta contra o dashboard, que passa `dir` da query string
    # direto para list_notes_in:
    #     dir=../../etc      -> HTTP 400  "Path outside the vault"
    #     dir=Proc%00edures  -> HTTP 500  "lstat: embedded null character in path"
    # A mesma pergunta com duas respostas, e a segunda vaza detalhe de
    # implementacao. Um caminho com byte nulo nao e mais utilizavel que um
    # `../../etc` e merece a mesma resposta limpa.
    try:
        target = (vault_root / rel_path).resolve()
        target.relative_to(vault_root.resolve())
    except (ValueError, OSError):
        return None
    return target


def safe_filename(title: str, max_len: int = 50) -> str:
    """Sanitize a title into a filesystem-safe filename stem.

    Truncation cuts on a word boundary when one is available, then strips
    punctuation the cut can strand. A raw slice used to leave stems like
    "... da arquitetura (" — a dangling opening paren that reads as a broken
    filename in Obsidian and labels the note's graph node with it.
    """
    safe = _INVALID_FILENAME_CHARS.sub("_", title)
    safe = re.sub(r"_+", "_", safe).strip().rstrip(" .")
    if len(safe) > max_len:
        safe = safe[:max_len]
        # Only back up to a word boundary if that keeps the stem recognisable;
        # a title with no spaces in range (or a very late first space) keeps
        # the hard slice rather than collapsing to a stub.
        cut = safe.rfind(" ")
        if cut >= max_len // 2:
            safe = safe[:cut]
        safe = safe.rstrip(" .,;:-_([{<\"'—–")
    return safe or "untitled"


def unique_note_path(dest: Path) -> Path:
    """Disambiguate a note destination that already exists on disk.

    write_note/export_session/maintenance-classify all derive the filename
    from just {date}-{safe title}.md — two notes with the same title on the
    same day would otherwise silently overwrite the first note's file *and*
    its ChromaDB index row, permanently losing its content. Appends -2, -3,
    ... before the suffix until a free path is found.
    """
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    n = 2
    while True:
        candidate = dest.with_name(f"{stem}-{n}{suffix}")
        if not candidate.exists():
            return candidate
        n += 1


#: Caracteres que um escalar YAML entre aspas duplas NAO aceita literais e que
#: por isso saem como `\xNN`. Medido varrendo os 256 primeiros codepoints: 64
#: deles quebram ou deformam o bloco, e sao exatamente estes mais o `\x85`.
_CONTROLE_YAML = {c for c in range(0x20)} | {0x7f} | set(range(0x80, 0xa1))


def yaml_quote_scalar(value: str) -> str:
    """Double-quote a string for safe use as a YAML frontmatter scalar value.

    An unquoted scalar containing ": " (colon-space) is ambiguous/invalid YAML
    (Obsidian and any strict frontmatter parser will choke on it), so this quotes
    unconditionally.

    E escapa os caracteres de CONTROLE, que a versao anterior nao escapava
    apesar de a docstring prometer seguranca "regardless of content". Achado por
    teste de propriedade em 04/09/2026: a propriedade

        yaml.safe_load("titulo: " + yaml_quote_scalar(s))["titulo"] == s

    caiu com `\x1f` em poucas dezenas de exemplos. Varrendo os 256 primeiros
    codepoints, SESSENTA E QUATRO quebram ou deformam o bloco, entre eles `\n` e
    `\r`. Um controle solto dentro de aspas duplas faz o leitor de YAML recusar
    o documento INTEIRO, que e o mesmo desfecho das duas notas deste vault com
    frontmatter ilegivel -- e esta funcao existe para impedir exatamente isso.

    Exposicao medida no vault na data: ZERO das 8.596 notas. Corrigido assim
    mesmo porque `create_note(title=...)` aceita qualquer string de qualquer
    chamador MCP, e `synthesize`/`classify` derivam titulo do CONTEUDO de
    documentos ingeridos: o zero de hoje e sobre o que ja entrou.

    Acento, travessao e emoji seguem literais: escapar controle nao pode virar
    escapar tudo, num vault escrito em portugues.
    """
    saida = []
    for ch in value:
        if ch == "\\":
            saida.append("\\\\")
        elif ch == '"':
            saida.append('\\"')
        elif ord(ch) in _CONTROLE_YAML:
            saida.append(f"\\x{ord(ch):02x}")
        else:
            saida.append(ch)
    return '"' + "".join(saida) + '"'


def yaml_unquote_scalar(value: str) -> str:
    """Reverse yaml_quote_scalar() for frontmatter values read back with naive line parsing.

    Decodifica na MESMA ordem em que a citacao escapou, e nao em passadas
    independentes: `\\\\x41` e uma barra invertida literal seguida de "x41", e nao
    a letra A. A versao anterior fazia dois `.replace()` encadeados e ja tinha
    esse problema para `\\\\"`; com `\\xNN` no meio ele fica visivel.
    """
    value = value.strip()
    if not (len(value) >= 2 and value[0] == '"' and value[-1] == '"'):
        return value
    corpo = value[1:-1]
    saida = []
    i = 0
    while i < len(corpo):
        ch = corpo[i]
        if ch != "\\" or i + 1 >= len(corpo):
            saida.append(ch)
            i += 1
            continue
        seguinte = corpo[i + 1]
        if seguinte == "x" and i + 3 < len(corpo):
            try:
                saida.append(chr(int(corpo[i + 2:i + 4], 16)))
                i += 4
                continue
            except ValueError:
                pass
        saida.append(seguinte)
        i += 2
    return "".join(saida)


_LEADING_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def frontmatter_aliases(content: str) -> set:
    """Return the set of Obsidian `aliases:` declared in a note's frontmatter.
    Supports both block-list and inline `[a, b]` forms. Empty set if none."""
    if not content.startswith("---\n"):
        return set()
    close = content.find("\n---\n", 4)
    if close == -1:
        return set()
    fm = content[4:close]
    # [^\S\n]* = horizontal whitespace only, so it never crosses the newline into
    # the first block-list item (the bug that swallowed `- item` into group 1).
    m = re.search(r"^aliases:[^\S\n]*(.*)$", fm, re.MULTILINE)
    if not m:
        return set()
    out: set = set()
    inline = m.group(1).strip()
    if inline.startswith("["):                       # aliases: [a, b]
        out |= {x.strip().strip('"').strip("'") for x in inline[1:-1].split(",")}
    else:                                            # block list under aliases:
        for line in fm[m.end():].splitlines():
            lm = re.match(r"[^\S\n]*-\s+(.*\S)", line)
            if lm:
                out.add(lm.group(1).strip().strip('"').strip("'"))
            elif line.strip() and not line[:1].isspace():
                break                                # next top-level key → stop
    return {a for a in out if a}


def _frontmatter_parses(content: str) -> bool:
    """Does this note's frontmatter block survive a real YAML parser?

    True for a note with no frontmatter at all: an absent block is not a broken
    one, and counting it would invent a defect in every plain note.

    PyYAML is a declared dependency of this package (pyproject: pyyaml>=6.0) and
    already imported lazily by sidecar.py and graph/manifest_ingest.py, so this
    adds no new requirement. Imported inside the function for the same reason
    they do it: the health scan is the only caller and it is not on the import
    path of the daemon's startup.
    """
    if not content.startswith("---"):
        return True
    # The same regex compose_note uses to find a caller's block, so "what counts
    # as frontmatter" has one definition here and is not re-guessed.
    m = _LEADING_FRONTMATTER_RE.match(content)
    if m is None:
        # Opens a block and never closes it. Not something YAML can be asked
        # about, and not something to report as a parse failure either: the
        # existing `truncated` metric is what covers a note cut off mid-write.
        return True
    try:
        import yaml
    except ImportError:      # pragma: no cover - declared dependency
        return True
    try:
        yaml.safe_load(m.group(1))
    except Exception:
        return False
    return True


def compose_note(title: str, content: str, date_str: str,
                 ai_generated: bool = True) -> str:
    """Build note text carrying exactly one YAML frontmatter block.

    Callers routinely include a full frontmatter block at the top of `content` —
    the AGENT_GUIDE's own "Content guidelines" section shows one — while this
    function also has generated fields to add. Concatenating both produced two
    stacked blocks, and since Obsidian parses only the first, every key the
    author actually wrote (subtitle, tags, aliases) silently became literal body
    text rendered after a horizontal rule.

    The caller's block is preserved verbatim rather than re-serialized, so block
    lists (`aliases:` with `- ` items) and any YAML this module doesn't model
    survive untouched. Generated keys are appended only where the caller did not
    already supply them — so an explicit `title:` in the content wins, which is
    what lets a note carry a short display title independent of its filename.
    """
    body = content.lstrip("\n")
    # `ai_generated` is a parameter rather than the constant "true" it was,
    # because the CLI's `note write` takes its content from a person — stdin or
    # --file — and stamped "false" by hand-rolling its own frontmatter instead
    # of coming through here. That divergence was what stopped `note write` from
    # being routed through the daemon like every other index write, and routing
    # it is the whole point: the CLI was opening ChromaDB as a second writer.
    #
    # This grants no new power. A caller supplying its own `ai_generated:` in
    # `content` already wins over the generated key by the merge rule below, so
    # the value was reachable; the parameter only makes it legible.
    generated = [
        ("title", yaml_quote_scalar(title)),
        ("date", date_str),
        ("ai_generated", "true" if ai_generated else "false"),
    ]

    # An alias carrying the untruncated title, when the filename could not.
    # safe_filename cuts the stem at 50 characters, so a note titled "Palworld —
    # mapa completo dos locais de mods no sistema" lands on disk as
    # "...-Palworld — mapa completo dos locais de mods no.md" and a link written
    # with the real title finds nothing. Stripping the date prefix at resolution
    # time handles the common case; it cannot restore characters the filename
    # never held. Added only when truncation actually happened, so the ordinary
    # note does not carry an alias identical to its own name.
    if len(safe_filename(title)) < len(safe_filename(title, max_len=10**6)):
        # Duas formas, porque links reais aparecem nas duas e a comparacao e
        # literal: get_health_summary faz `key = link.lower()` e procura em
        # `resolvable`, sem tirar prefixo de data do ALVO. link_names_for_stem
        # tira a data do STEM, o que cobre o link sem data para uma nota nao
        # truncada, e nao ajuda em nada aqui.
        #
        # Medido neste vault: dos seis alvos quebrados que apontam para notas
        # reais, SEIS usam a forma com data ("2026-08-07-Dailies Sotéria ...").
        # Um alias so com o titulo nu nao resolveria nenhum deles, que era o
        # defeito da primeira versao desta correcao.
        aliases = [f"{date_str}-{title}", title]
        generated.append(("aliases",
                          "".join("\n  - " + yaml_quote_scalar(a) for a in aliases)))

    # A block-list value carries its own newline, so it must not get the space a
    # scalar needs after the colon — "aliases: \n" leaves trailing whitespace on
    # a line that every YAML linter and half the diff tools flag.
    def _emit(k, v):
        return f"{k}:{v}" if v.startswith("\n") else f"{k}: {v}"

    m = _LEADING_FRONTMATTER_RE.match(body)
    if not m:
        block = "\n".join(_emit(k, v) for k, v in generated)
        return f"---\n{block}\n---\n\n{body}"

    supplied = m.group(1)
    # Top-level keys only: indented lines and `- ` items belong to a block list.
    have = {
        line.split(":", 1)[0].strip().lower()
        for line in supplied.splitlines()
        if ":" in line and line[:1] not in (" ", "\t", "-")
    }
    additions = [_emit(k, v) for k, v in generated if k not in have]
    merged = supplied + ("\n" + "\n".join(additions) if additions else "")

    # `aliases` is the one generated key that must MERGE rather than defer.
    #
    # For every other key, "the caller wins" is right: an explicit `title:` is
    # how a note carries a short display name independent of its filename. But
    # the generated alias is not an opinion about naming, it is the bridge back
    # to a title the filename could not hold. Deferring to the caller's list
    # dropped it entirely, and the note became unlinkable by its own title.
    #
    # Measured on a real vault: two notes whose titles run past the 50-character
    # filename limit each supplied two descriptive aliases of their own, so
    # neither got the truncation alias, and both are linked-to by their full
    # titles from other notes. Those links resolve to nothing.
    if "aliases" in have:
        for chave, valor in generated:
            if chave == "aliases":
                for item in valor.strip().split("\n"):
                    item = item.strip().lstrip("- ").strip()
                    if item:
                        merged = _merge_alias(merged, item)
                break

    return f"---\n{merged}\n---\n\n{body[m.end():].lstrip(chr(10))}"


def _merge_alias(frontmatter: str, alias: str) -> str:
    """Add `alias` to an existing `aliases:` value, block-list or inline.

    The caller's block is preserved verbatim everywhere else in compose_note, so
    this inserts a line (or one bracket entry) rather than re-serialising YAML
    that this module does not model.
    """
    # O alias chega das duas formas: cru de um chamador, e JA CITADO quando vem
    # do bloco que `compose_note` gerou com yaml_quote_scalar. A funcao ja sabia
    # disso -- despia as aspas para COMPARAR -- e inseria a forma crua assim
    # mesmo. Citar os dois casos sem despir produziu `"\"Titulo\""`, alias
    # duplamente citado que nao resolve, e a suite existente pegou na hora.
    # Despir com yaml_unquote_scalar, que e o inverso exato de quem cita.
    cru = yaml_unquote_scalar(alias.strip())
    despido = cru.strip("'").lower()
    linhas = frontmatter.split("\n")
    for i, linha in enumerate(linhas):
        if not linha.lower().startswith("aliases:"):
            continue

        inline = linha.split(":", 1)[1].strip()
        if inline.startswith("[") and inline.endswith("]"):
            # aliases: [A, B]
            dentro = inline[1:-1].strip()
            existentes = {x.strip().strip('"').strip("'").lower()
                          for x in dentro.split(",") if x.strip()}
            if despido in existentes:
                return frontmatter
            # Citado, e nao interpolado cru. Um alias com virgula virava DOIS
            # aliases, um com `]` invalidava o bloco inteiro, e um com ": "
            # virava um dicionario dentro da lista. `yaml_quote_scalar` esta
            # quinze linhas acima neste mesmo arquivo e diz no docstring que
            # existe para isto.
            citado = yaml_quote_scalar(cru)
            novo_inline = f"[{dentro}, {citado}]" if dentro else f"[{citado}]"
            linhas[i] = f"aliases: {novo_inline}"
            return "\n".join(linhas)

        # aliases:\n  - "A"\n  - "B"
        j = i + 1
        while j < len(linhas) and (linhas[j].startswith((" ", "\t"))
                                   or linhas[j].lstrip().startswith("- ")):
            item = linhas[j].strip().lstrip("- ").strip().strip('"').strip("'").lower()
            if item == despido:
                return frontmatter
            j += 1
        # O ramo de bloco e imune a virgula e nao aos outros dois: ": " fazia o
        # item virar mapa, e `#` truncava no comentario ("Sprint #3 do time"
        # virava "Sprint"). Mesma citacao, pelos mesmos motivos.
        linhas.insert(j, f"  - {yaml_quote_scalar(cru)}")
        return "\n".join(linhas)
    return frontmatter


def ingested_link_stems(registro: dict) -> set[str]:
    """Nomes pelos quais um arquivo ingerido de fora do vault pode ser linkado.

    Le o registro de ingestao em vez de varrer disco: o registro ja guarda o
    caminho de cada arquivo indexado, entao a resposta sai sem tocar em 1.700
    arquivos espalhados por 23 fontes.

    O registro reflete a ULTIMA ingestao, nao o disco de agora, e isso e o
    comportamento correto: se um arquivo ainda nao foi ingerido, o
    `search_vault(scope='external')` tambem nao o encontra, entao o link
    realmente aponta para algo que o servidor nao serve. A checagem se cura
    sozinha quando a pasta e reingerida.

    Recebe o registro em vez de le-lo: quem le e o `vault`, que fica acima do
    `ingest` na hierarquia de modulos. Ler daqui fazia o `notes`, a camada mais
    baixa, depender do `ingest`, e fechava um ciclo de import.
    """

    nomes: set[str] = set()
    for entrada in registro.values():
        if not isinstance(entrada, dict):
            continue
        for caminho in (entrada.get("files") or {}):
            stem = Path(caminho).stem
            if stem:
                nomes.update(link_names_for_stem(stem))
    return nomes


# ── estado do indice (.chroma_index.json) ────────────────────────────────────
#
# Este arquivo diz quais notas ja estao embutidas e com que mtime, e e o que o
# reindex incremental consulta para pular o que nao mudou. Ele morava inteiro
# no `vault.py` com escrita atomica e SEM trava, e as duas coisas tem nomes
# parecidos e resolvem problemas diferentes: atomica garante que ninguem le
# pela metade, trava garante que quem leu antes de voce ainda esteja no que
# voce grava.
#
# O defeito medido em 26/09/2026: os tres escritores (`stamp_indexed`,
# `delete_notes` e o fim do `reindex_vault`) fazem ler-modificar-escrever, e os
# hooks de sessao criam concorrencia de verdade entre eles. O de fim de sessao
# dispara `delegation-core reindex`, o de inicio dispara `delegation-core
# maintain`, e `jobs.submit` abre uma thread nova sem checar se ja existe um
# job igual rodando. Duas sessoes fechando juntas, ou uma fechando enquanto
# outra abre, colocam dois escritores no mesmo arquivo.
#
# Consequencia, na propria letra do docstring que ja estava la: o ultimo a
# gravar escreve por cima e os carimbos do outro somem, e o proximo reindex
# reembute nota intocada. E o nome do temporario era fixo, entao dois
# escritores abriam o MESMO `.json.tmp`: um truncava o do outro no meio do
# dump e o `os.replace` instalava o resultado, que `carregar` le como "nada
# esta carimbado", ou seja um reembute do vault inteiro.
#
# E a mesma forma de defeito que o `locking.py` existe para conter, e que ja
# tinha sido corrigida no `jobs.py` em 03/09 e no registro de ingestao em
# 31/08. Aqui era o terceiro sitio.

ESTADO_DO_INDICE = ".chroma_index.json"


def caminho_do_estado(vault: Path) -> Path:
    return Path(vault) / ESTADO_DO_INDICE


def carregar_estado(caminho: Path) -> dict:
    """Devolve {} para arquivo ausente ou ilegivel, avisando no segundo caso.

    Responder {} e correto: toda nota parece sem carimbo e o proximo passe a
    reembute, o que e lento e nao errado. O que nao pode e ser silencioso, que
    era o comportamento antes, porque o SINTOMA e um reindex "incremental" que
    roda por minutos sem nada dizendo por que.
    """
    caminho = Path(caminho)
    if caminho.exists():
        try:
            return json.loads(caminho.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(
                "estado do indice em %s ilegivel (%s): toda nota sera tratada "
                "como sem carimbo e reembutida no proximo reindex", caminho, e)
    return {}


def gravar_estado(caminho: Path, estado: dict) -> None:
    """Escrita atomica, com temporario UNICO por processo e thread.

    O nome fixo `.json.tmp` era compartilhado: sob trava isso nao morde, mas o
    temporario e a ultima linha de defesa de quem grave este arquivo por um
    caminho que a trava nao cubra, e um nome unico custa uma linha.
    """
    caminho = Path(caminho)
    tmp = caminho.with_suffix(f".json.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(estado, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, caminho)
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        logger.warning("nao foi possivel gravar o estado do indice: %s", e)


def atualizar_estado(caminho: Path, modificar) -> dict:
    """Ler, modificar e gravar sob UMA trava, que e o ponto desta funcao.

    `modificar` recebe o estado em disco e devolve o que vai ser gravado.

    Quando a trava nao sai no tempo de espera, o ciclo roda sem ela e avisa: um
    carimbo perdido custa reembutir a nota, e derrubar a escrita da nota (ou o
    fim de uma sessao) por causa de um carimbo seria pior que o defeito.
    """
    caminho = Path(caminho)
    try:
        return locking.ler_modificar_escrever(
            caminho,
            ler=lambda: carregar_estado(caminho),
            escrever=lambda novo: gravar_estado(caminho, novo),
            modificar=modificar,
        )
    except locking.TravaIndisponivel as e:
        logger.warning("%s: seguindo sem trava, um carimbo pode se perder", e)
        novo = modificar(carregar_estado(caminho))
        gravar_estado(caminho, novo)
        return novo
