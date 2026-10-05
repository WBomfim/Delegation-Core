# delegation-core — Deployment Log (multi-implementation)

**Purpose.** delegation-core is not a single linear install. It runs as several
independent field implementations (named here field deployment A, B, and so on) on different hardware, with
different local models, prompt languages, and locally-grown hardening. The
`CHANGELOG.md` describes the *package* lineage (v5 → v5.1) and implicitly assumes
every install shares that lineage. **It does not.** Some deployments are on
divergent pre-refactor branches and never had a v5 baseline, so a release the
changelog calls a "drop-in patch" is, for them, a **major refactor migration**.

This file gathers per-implementation upgrade logs so those differences are visible
in one place. Each deployment appends its own dated entry when it upgrades. Do not
overwrite another deployment's entry.

---

## Field deployment D (Windows): ingestion configuration and startup fixes, 2026-09-24

**Host / profile.** Windows local installation using the dedicated
`.delegation_core` virtual environment. The installed package reports version
`0.13.0`.

**Delivered changes.**
- Configured the declarative ingestion workflow with allow-listed sources and
  exclusions.
- Recreated the Windows Startup launcher so it invokes the virtual environment
  Python runtime instead of the blocked legacy executable.
- Verified distinct vault Markdown-file, indexed-document, and ChromaDB-chunk
  metrics.

**Validation.**
- Focused integration validation passed 138 tests.
- The Startup launcher invoked the virtual environment Python runtime.
- An authenticated MCP request completed successfully after service startup.

**Runtime source transition.** The previous runtime resolved to
`Delegation-Core-Office` through its installation metadata. It was reinstalled
the same day from a local checkout of a contributor fork of
`AnonJoey/Delegation-Core-Office`; the active runtime now resolves to that
checkout. The service was reinstalled and an authenticated MCP initialization
returned HTTP 200 on `127.0.0.1:8787`.

**Rollback.** Reinstall the previously validated checkout and restart the
service. No vault migration or index rebuild is required.

---

## Field deployment A: v6 linking redesign, 2026-07-03

**Trigger.** Post-v5.1 testing of `relink_folder` on this vault (442 notes). A
dry-run instead of a live call exposed that the tool was unsafe.

**What the metric claimed vs reality.** `heartbeat.vault_health` reported **259
broken links / 260 orphans**. Investigation showed this is **~98% false
positives**:
- Only **4** broken links were actually in generated `## Related` sections.
- **245** "broken links" were in note *bodies* — ingested content, not links we
  wrote: 112 imported Obsidian path-links (`[[3. Brand Platform/Purpose/Sinek.pdf]]`),
  84 prose fragments, 49 slugs, and bash `[[ -f "$x" ]]` **test expressions** from
  ingested shell scripts. The health scanner counted every `[[...]]` as a wikilink.
- `orphans` just meant "note has no `## Related` heading", inflated by the
  frontmatter-less `decisions/` and `reference/` folders.

**The dangerous part.** `relink_folder` generated links from the indexed *title*
while the vault is addressed by filename *stem*. Faithful read-only dry-run on
`insights/`: it would touch all 53 notes, add 418 links, **84% of them broken** —
nearly tripling the vault's broken-link count. A naive "semantically repair the
251 broken links" would have rewritten bash scripts and PDF references into fake
note links. Both avoided by dry-running + verifying first.

**Root causes (two code defects, fixed in v6).**
1. Two link vocabularies: `wikilinks()` linked by stem (resolves),
   `relink_folder()` linked by title (breaks). Unified — everything links by stem.
2. Health scanner had no code/shell/path exclusion and no alias awareness.

**v6 design — "resolve by stem, display by title."**
- Aliased links `[[stem|Clean Title]]` from one shared `format_link()` used by all
  generators. Verified: `insights` goes 16% → **100%** resolvable.
- Obsidian `aliases:` frontmatter written at synthesis (`ensure_aliases`), so
  legacy `[[Title]]`/`[[slug]]` links resolve natively.
- Health counting: strip code + shell `[[ ]]`, resolve against stems ∪ aliases,
  true-graph-orphan definition. broken_links becomes truthful.
- **No file renames** (would break ChromaDB paths + existing links).

**Vault migration (full, all 442 notes).** File-only pass (no ChromaDB writes —
the live server holds the collection open; a reindex runs after restart). Backup:
`Business_Vault_backup_20260703-152149` (442 .md).

Results:
- **438/442 notes** received an Obsidian `aliases:` entry (readable title).
- **142 generated `## Related` links** across 55 notes reformatted to aliased
  `[[stem|Display]]`.
- **4 dangling `## Related` links** left untouched (no target note exists —
  `[[Q2 Operations Tooling]]`, `[[Infrastructure Strategy]]`, …) — flagged for
  manual review, not auto-invented.
- Body `[[...]]` (imported content) untouched. **Zero new broken links introduced.**

Health metric, before → after v6 (same vault, honest counting):
`broken_links 259 → 91` (de-pollution), `orphans 260 → 302` (redefinition:
"missing ## Related" → true graph orphan). The remaining 91 broken are body-content
artifacts (imported slugs/prose), left as content by design.

**Two bugs caught during migration (dry-run + spot-check + before/after diff), both
fixed before the final apply:**
1. `ensure_aliases` didn't dedup within the incoming list → duplicate aliases.
2. **`Path.stem` truncated dotted filenames** (`..._pt.02.md` → `..._pt`) — and
   **382/442 notes have dots in their stem**, so this mis-identified the majority
   of the vault (in `format_link`, the health resolvable set, splitter siblings,
   and the migration index). Fixed everywhere via a `_to_stem` helper / `name[:-3]`
   that strips only `.md`. This was the single deepest root cause.

**Follow-ups — completed same day (post-restart, v6 live):**
- **YAML frontmatter repair:** 35 notes had strict-invalid frontmatter (unquoted
  `title:` with a colon; `tags: [#hashtag]` flow sequences read as comments).
  Quoted the offending scalars/list-elements; re-validated. **35/35 fixed, 0 left.**
- **Graph densification:** ran a controlled pass (MIN_SIM 0.66, ≤6 links/note) adding
  aliased `[[stem|Display]]` cross-links. **2599 links across 439 notes; orphans
  302 → 36.** Backup: `Business_Vault_backup_predensify_20260703-153918`.
- **Third bug caught + fixed:** a few ingested files have **trailing-space names**
  (`...PROPOSTA - `); the health check stripped link targets but not the resolvable
  stems, false-flagging valid links as broken. Made stem resolution strip-consistent.
- **ChromaDB re-synced** twice via `vault_reindex_bg` (migration + densification were
  file-only to stay clear of the live server's open collection).

**Final v6 health:** `total 442, broken_links 91, orphans 36, needs_repair 0`
(from `broken_links 259 / orphans 260` before v6). Remaining 91 broken are
body-content artifacts (imported slugs/prose), left as content by design.

---

## Field deployment A: upgrade to v5.1, 2026-07-03

**Host / profile.** macOS, Intel i9-9880H, CPU-only (no GPU/Metal). Local model
Llama-3.2-1B-Instruct-Q4_K_M @ llama.cpp port 8181 (launchd `com.delegation-core.llama`).
Sole MCP across Claude Code + Claude Desktop. Vault: `/Users/<user>/Business_Vault`
(440 notes). Install mode: editable (`pip install -e .`) at
`/Users/<user>/.../delegation_core`.

**Starting point — NOT v5.** This box was on a divergent **`0.1.0`** carrying local
"hardening round 2" (documented in `DIVERGENCE.md`). It never went through v5, so
the changelog's "drop-in patch over v5 / upgrade in place" instruction did **not**
apply as written. Actual jump: `0.1.0 (divergent) → 0.5.1`, across the module
refactor (monolith → 11 modules: junk, merger, linker, synthesizer, splitter,
classifier, embeddings, ingest, jobs, session, sidecar).

**Local hardening — mostly UPSTREAMED (verified in v5.1 code).**
- `JUNK_STEM_RE` + `~$` Office-lock filter → `junk.py` (credited to field deployment A).
- `FOLDER_HINTS` + neutral `reference/` classifier fallback → `classifier.py`.
- Merge size-guards (incoming 32KB / target 150KB) → `merger.py`.
- Filename collision suffixes, full note-path in `results["classified"]` → present.
- `vault_folder:` routing → **mechanism changed**: moved from inline YAML frontmatter
  to `<stem>.meta.yaml` **sidecar** files (`sidecar.py`, "field deployment B").
- ChromaDB telemetry: old env-var + posthog-logger silence (0.1.0 `__init__.py`)
  replaced by `anonymized_telemetry=False` on the client (`vault.py`) — cleaner root
  fix, so the old `__init__.py` hack was intentionally NOT carried forward.

**Downgrade found + ported forward.** v5.1 dropped the per-file **merge opt-out**
(0.1.0 `vault_merge: false` frontmatter); only automatic size-based suppression
remained. Restored in `organizer.py` via `_merge_forbidden(sc, raw_text)`, gated at
both `try_merge` call sites (main path + `_process_sections`, threaded via a
`no_merge` param). Honors BOTH the v5.1-native sidecar key `no_merge: true` AND the
legacy inline `vault_merge: false`. Documented in `sidecar.py`. Unit-tested 7/7.

**Config compatibility.** `Config.load()` filters to known dataclass fields, so the
existing `~/.delegation_core/config.json` loaded unchanged; new `engine_mode`
defaulted to `local` (= prior behavior). No model re-download. `search_web` correctly
inert (`[web]`/ddgs extra not installed, flag off).

**Dependency note.** `setuptools` is `82.0.1` on this venv — above the changelog's
new `<82` torch pin — but torch 2.2.2 + sentence-transformers import fine because
they were already built compatibly. The pin only bites on a *fresh* `install.sh`
run; an in-place `pip install -e .` (as done here) does not touch setuptools.

**Obstacle / gotcha for the next upgrader.** The long-running MCP server keeps the
OLD code in memory after an in-place install. Confirmed here: post-install
`heartbeat` still returned the `0.1.0` shape (no `engine_mode` field). Reload
requires fully quitting **both** Claude Code and Claude Desktop (each stdio-spawns
its own `delegation-core run`). The `llama-server` launchd job needs no restart.
Verify the reload succeeded by checking `heartbeat` now reports `engine_mode`.

**Backup.** Pre-upgrade working tree + config snapshot at
`delegation_core_backup_20260703-143932/`.

---

## JOEY deployment — v6.4 GitHub repository release — 2026-07-21

**Action.** Initialized Git workflow and deployed `delegation_core_v6.4` to a dedicated public GitHub repository.

**Details.**
- **Local Repository:** `/home/joey/Projects/delegation-core`
- **Git User Configured:** `joey bernardes <followthepillow@gmail.com>`
- **GitHub Repository Created:** [https://github.com/AnonJoey/delegation-core-v6.4](https://github.com/AnonJoey/delegation-core-v6.4)
- **Tooling Used:** GitHub CLI (`gh` v2.96.0) installed and authenticated to `AnonJoey`.
- **Status:** Initial `v6.4` release commit pushed to `origin/master`.

---

<!-- Next deployment: append your entry above this line, newest first under its own
     "## <NAME> deployment — <action> — <date>" header. -->

