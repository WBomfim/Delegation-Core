"""
installer.py — install, update and uninstall as one implementation.

Until now this project had no `update` at all. Updating meant re-running
install.sh or install.bat, which those scripts do handle (they back up the
previous package, keep config.json, and re-register the service), but with two
gaps that only show up on a machine that is already running:

  1. Neither script STOPS the daemon before `pip install` writes into the venv
     the daemon is running from. On Linux and macOS that survives, and both
     scripts print "Restart delegation-core to load the new code", so the user
     is left holding a manual step. On Windows pip cannot replace a file another
     process holds open, and the line that follows is `>nul 2>&1` with no
     errorlevel check, so a half-applied upgrade has no way of being noticed.

  2. The same six tasks are written twice, once in bash and once in batch, and
     they have already drifted: uninstall.sh checks the result of what it
     removes, uninstall.bat does not.

This module is the single implementation. `update()` lands first because it is
the operation that did not exist; install and uninstall follow into the same
place, and the shell scripts shrink to finding a Python and calling in.

Nothing here touches the vault or the downloaded model weights. Ever.
"""

from __future__ import annotations

import filecmp
import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path

from . import service
from .config import CONFIG_DIR

logger = logging.getLogger("installer")

#: Documents copied to CONFIG_DIR so the MCP clients can point at a stable path
#: regardless of where the source checkout lives.
SHIPPED_DOCS = ("AGENT_GUIDE.md", "CLAUDE_SYSTEM_PROMPT.md")


# ── where this install came from ─────────────────────────────────────────────

def _dist_info() -> Path | None:
    """The installed package's .dist-info directory, or None."""
    for parent in Path(__file__).resolve().parents:
        if parent.name == "site-packages":
            hits = sorted(parent.glob("delegation_core-*.dist-info"))
            return hits[0] if hits else None
    # Editable installs put the package outside site-packages, so walk the
    # interpreter's own path instead of the module's.
    for entry in sys.path:
        candidato = Path(entry)
        if candidato.name == "site-packages":
            hits = sorted(candidato.glob("delegation_core-*.dist-info"))
            if hits:
                return hits[0]
    return None


def source_root() -> Path | None:
    """The directory this package was installed FROM, or None if unknowable.

    Read from `direct_url.json`, which pip writes per PEP 610 whenever a
    package is installed from a path or a VCS. On this machine it holds
    `{"dir_info": {"editable": true}, "url": "file:///home/joey/Projects/delegation-core"}`.

    Deliberately not guessed from `__file__`: that works for an editable
    install, where the module really does live in the checkout, and gives the
    wrong answer for a normal one, where it points into site-packages and the
    original source directory is not derivable from it at all. A wrong source
    root here would make `update()` pull in the wrong place, or reinstall the
    copy it is trying to replace.
    """
    info = _dist_info()
    if info is None:
        return None
    try:
        dados = json.loads((info / "direct_url.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    url = str(dados.get("url", ""))
    if not url.startswith("file://"):
        return None                      # installed from an index or a VCS URL
    caminho = Path(url[len("file://"):])
    return caminho if caminho.is_dir() else None


def is_editable() -> bool:
    """True when the venv points at the source tree instead of copying it."""
    info = _dist_info()
    if info is None:
        return False
    try:
        dados = json.loads((info / "direct_url.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(dados.get("dir_info", {}).get("editable"))


# ── git, when the source is a checkout ───────────────────────────────────────

def _git(root: Path, *args: str, timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", "-C", str(root), *args],
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, "git not found"
    except subprocess.TimeoutExpired:
        return 124, f"git timed out after {timeout}s"


def is_git_checkout(root: Path) -> bool:
    return (root / ".git").exists()


def git_state(root: Path) -> dict:
    """Branch, commit and how far behind its upstream, without changing anything.

    `fetch` is a read: it updates the remote-tracking ref and touches no file in
    the working tree. Counting commits without fetching first would report
    "up to date" from a stale ref, which is the answer a user is least able to
    detect as wrong.
    """
    if not is_git_checkout(root):
        return {"git": False}

    _, branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    _, commit = _git(root, "rev-parse", "--short", "HEAD")
    sujo = _git(root, "status", "--porcelain")[1]

    codigo_fetch, saida_fetch = _git(root, "fetch", "--quiet")
    upstream_code, upstream = _git(root, "rev-parse", "--abbrev-ref", "@{upstream}")

    atras = adiante = None
    if upstream_code == 0:
        codigo, contagem = _git(root, "rev-list", "--left-right", "--count",
                                f"{upstream}...HEAD")
        if codigo == 0 and "\t" in contagem:
            esq, dir_ = contagem.split("\t")[:2]
            atras, adiante = int(esq), int(dir_)

    return {
        "git": True,
        "branch": branch,
        "commit": commit,
        "dirty": bool(sujo.strip()),
        "dirty_files": len([ln for ln in sujo.splitlines() if ln.strip()]),
        "upstream": upstream if upstream_code == 0 else None,
        "behind": atras,
        "ahead": adiante,
        "fetch_ok": codigo_fetch == 0,
        "fetch_detail": "" if codigo_fetch == 0 else saida_fetch,
    }


# ── shipped docs and hooks ───────────────────────────────────────────────────

def refresh_shipped_files(root: Path) -> dict:
    """Copy AGENT_GUIDE/CLAUDE_SYSTEM_PROMPT and the hooks into CONFIG_DIR.

    Never clobbers a file the user changed: theirs is kept and the shipped copy
    lands beside it as `<name>.dist.<ext>` so the two can be diffed on purpose.

    **Compares before declaring a customisation**, which is where install.sh
    was wrong. Its hook loop used `cmp -s` and only wrote `.dist.py` on a real
    difference; its document helper wrote `.dist.md` whenever the destination
    merely existed. So every upgrade told the user "AGENT_GUIDE.md already
    present: kept yours" about a file they had never touched, and left a
    redundant copy behind as if there were something to reconcile.

    Measured on this machine before the fix: `AGENT_GUIDE.md` and
    `AGENT_GUIDE.dist.md` were byte-identical at 35.857 bytes, and
    `CLAUDE_SYSTEM_PROMPT.md` and its `.dist.md` at 6.148. The hooks directory,
    doing the same job correctly, had produced no `.dist.py` at all.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (CONFIG_DIR / "hooks").mkdir(parents=True, exist_ok=True)
    resultado: dict = {"installed": [], "kept_yours": [], "unchanged": [], "missing": []}

    def _copiar(origem: Path, destino: Path, rotulo: str) -> None:
        if not origem.is_file():
            resultado["missing"].append(rotulo)
            return
        if not destino.exists():
            shutil.copyfile(origem, destino)
            resultado["installed"].append(rotulo)
            return
        if filecmp.cmp(origem, destino, shallow=False):
            resultado["unchanged"].append(rotulo)
            return
        lado = destino.with_suffix(f".dist{destino.suffix}")
        shutil.copyfile(origem, lado)
        resultado["kept_yours"].append(rotulo)

    for nome in SHIPPED_DOCS:
        _copiar(root / nome, CONFIG_DIR / nome, nome)

    for hook in sorted((root / "hooks").glob("*.py")) if (root / "hooks").is_dir() else []:
        _copiar(hook, CONFIG_DIR / "hooks" / hook.name, f"hooks/{hook.name}")

    return resultado


def stale_dist_copies() -> list[str]:
    """`.dist` files that are identical to the file they sit beside.

    These are the leftovers of the bug above: nothing to reconcile, but they
    read as if there were. Reported rather than deleted, because a `.dist` file
    is the user's to remove.
    """
    sobras = []
    for base in CONFIG_DIR.glob("*.dist.*"):
        real = base.with_name(base.name.replace(".dist", "", 1))
        if real.is_file() and filecmp.cmp(base, real, shallow=False):
            sobras.append(str(base.relative_to(CONFIG_DIR)))
    for base in (CONFIG_DIR / "hooks").glob("*.dist.*"):
        real = base.with_name(base.name.replace(".dist", "", 1))
        if real.is_file() and filecmp.cmp(base, real, shallow=False):
            sobras.append(str(base.relative_to(CONFIG_DIR)))
    return sorted(sobras)


# ── the update itself ────────────────────────────────────────────────────────

def _pip_install(root: Path) -> tuple[bool, str]:
    """Reinstall the package from `root`, with the optional extras if they build."""
    base = [sys.executable, "-m", "pip", "install", "--quiet"]
    for alvo in (f"{root}[graph,web]", str(root)):
        try:
            p = subprocess.run([*base, alvo], capture_output=True, text=True, timeout=1800)
        except subprocess.TimeoutExpired:
            return False, "pip timed out after 30 minutes"
        if p.returncode == 0:
            return True, alvo
        ultimo = (p.stdout + p.stderr).strip()
    return False, ultimo


def update(check_only: bool = False, restart: bool = True,
           root: Path | None = None) -> dict:
    """Bring the installed package up to date with its source, safely.

    The order is the point:

      1. find the source, and refuse rather than guess if it cannot be found;
      2. report what would change; stop here when `check_only`;
      3. STOP the daemon, so pip is not writing under a live process;
      4. pull, reinstall, refresh the shipped docs and hooks;
      5. re-register the service, then start it and wait until it answers.

    A step that fails stops the sequence, and the daemon is put back up
    regardless. An update that fails halfway must not leave the machine with no
    service: that would be worse than not having updated at all.

    `restart=False` leaves the daemon stopped at the end, for a caller that is
    about to do more work before bringing it back.
    """
    raiz = root or source_root()
    passos: list[dict] = []

    def _passo(nome: str, ok: bool, **extra) -> dict:
        registro = {"step": nome, "ok": ok, **extra}
        passos.append(registro)
        return registro

    if raiz is None:
        return {
            "status": "cannot_locate_source",
            "steps": passos,
            "detail": (
                "This install has no recorded source directory (pip's "
                "direct_url.json is absent or does not point at a path). "
                "Download a fresh copy and run the installer in it."
            ),
        }

    from . import __version__ as versao_antes
    estado = git_state(raiz)
    _passo("locate_source", True, root=str(raiz), editable=is_editable(), **estado)

    if check_only:
        atras = estado.get("behind")
        return {
            "status": ("behind" if atras else "up_to_date") if estado.get("git")
                      else "unknown_not_a_checkout",
            "version": versao_antes,
            "root": str(raiz),
            "steps": passos,
        }

    if estado.get("git") and estado.get("dirty"):
        return {
            "status": "refused_dirty_checkout",
            "steps": passos,
            "detail": (
                f"{estado['dirty_files']} uncommitted change(s) in {raiz}. "
                "Committing or stashing them is your call, not this command's: "
                "a pull here could conflict or discard work."
            ),
        }

    estava_no_ar = service.is_up()
    parada = service.stop()
    if parada["status"] == "failed":
        _passo("stop_service", False, **parada)
        return {"status": "failed", "steps": passos,
                "detail": "Refusing to write into the venv while the daemon is still up."}
    _passo("stop_service", True, **parada)

    resultado = {"status": "ok", "version_before": versao_antes, "root": str(raiz)}
    try:
        if estado.get("git"):
            codigo, saida = _git(raiz, "pull", "--ff-only")
            _passo("git_pull", codigo == 0, detail=saida)
            if codigo != 0:
                resultado["status"] = "failed"
                resultado["detail"] = (
                    "git pull --ff-only failed. Fast-forward only is deliberate: "
                    "merging or rebasing your branch is your decision.\n" + saida
                )
                return resultado

        ok, detalhe = _pip_install(raiz)
        _passo("pip_install", ok, detail=detalhe)
        if not ok:
            resultado["status"] = "failed"
            resultado["detail"] = f"pip install failed:\n{detalhe}"
            return resultado

        _passo("refresh_docs_and_hooks", True, **refresh_shipped_files(raiz))
        _passo("register_service", True, **service.install())
    finally:
        # Whatever happened above, the machine does not get left without a
        # daemon it had before this command ran.
        if restart and estava_no_ar:
            partida = service.start()
            pronto = service.is_up(wait_seconds=60)
            _passo("start_service", partida["status"] == "started" and pronto,
                   ready=pronto, **partida)
            if not pronto:
                resultado["status"] = "started_but_not_answering"
                resultado.setdefault(
                    "detail",
                    "The service manager accepted the start but the daemon did "
                    "not answer on its port within 60s. Check its log.",
                )

    resultado["steps"] = passos
    resultado["stale_dist_copies"] = stale_dist_copies()
    return resultado


# ── uninstall ────────────────────────────────────────────────────────────────

#: Directories removed from CONFIG_DIR. `models` is not here, and never will be.
REMOVE_DIRS = ("sessions", "graphs", "llama", "hooks", "app")

#: Files removed from CONFIG_DIR.
REMOVE_FILES = (
    "config.json", "processes.json", "graphs_registry.json",
    "last_brief.json", "vault_health.json",
    "AGENT_GUIDE.md", "AGENT_GUIDE.dist.md",
    "CLAUDE_SYSTEM_PROMPT.md", "CLAUDE_SYSTEM_PROMPT.dist.md",
)

#: Glob patterns removed from CONFIG_DIR.
REMOVE_GLOBS = ("*.log", "backups_pre_upgrade_*")

#: Never removed, by anything, for any reason. The vault is the other one, and
#: it is not listed here only because it does not live under CONFIG_DIR.
KEEP_ALWAYS = ("models", "venv")

#: Written when `uninstall()` finishes and leaves the venv standing, read by the
#: shell wrapper that then removes it. A file rather than an exit code or a
#: printed line: exit 0 also means "the user typed something other than yes at
#: the prompt", and a wrapper that deleted the venv on exit 0 would destroy the
#: install of someone who had just declined to uninstall it.
VENV_PENDING_NAME = ".venv-pending-removal"


def venv_pending_path() -> Path:
    """Where the sentinel lives, resolved when asked and not at import.

    A module-level `CONFIG_DIR / name` would freeze the directory at import
    time. That is the exact shape of the bug this project already paid for once
    with `config.CONFIG_FILE`: every `from .config import CONFIG_DIR` holds its
    own copy, so redirecting the source module leaves the copies pointing at the
    user's real home. Here that would mean a test writing a sentinel into a live
    installation.
    """
    return CONFIG_DIR / VENV_PENDING_NAME


def configured_vault_path() -> str:
    """The vault path recorded in config.json, or "" if unreadable.

    Read directly rather than through `Config.load()`: this runs during an
    uninstall, where a config that fails to validate must still yield whatever
    path it holds. The point of reading it is to tell the user where their vault
    still is, and to refuse the uninstall if it sits somewhere unsafe.
    """
    try:
        dados = json.loads((CONFIG_DIR / "config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    valor = dados.get("vault_path", "")
    return valor if isinstance(valor, str) else ""


def vault_is_inside_config_dir(vault: str) -> bool:
    """Would removing CONFIG_DIR's contents also remove vault content?

    Someone who answered "~/.delegation_core" (or a subfolder) at setup has a
    vault whose own files can be named `sessions/`, `graphs/`, `config.json`.
    Every removal below is by exact name, so on that machine they are not
    targeted removals at all.

    Resolves symlinks on both sides: a vault symlinked into CONFIG_DIR is the
    same hazard, and comparing the literal strings would miss it.

    A path that cannot be resolved answers True, not False. This used to say
    "no hazard" about a path it had failed to examine, and it is the gate that
    decides whether `uninstall()` proceeds to remove by exact name. The cost of
    a wrong True is that someone has to move their vault and retry; the cost of
    a wrong False is `rm -rf` on `sessions/` in a directory that turned out to
    hold their notes. `wizard._conflicts_with_config_dir` is the same check on
    the way in and had the same defect.
    """
    if not vault:
        return False
    try:
        v = Path(vault).expanduser().resolve()
        c = CONFIG_DIR.expanduser().resolve()
    except OSError:
        return True
    return v == c or c in v.parents


def config_is_unreadable() -> bool:
    """True when config.json is there and cannot be parsed.

    `configured_vault_path()` flattens four different situations into `""`:
    no config file, an unreadable one, invalid JSON, and a config with no
    vault_path. Three of those mean "there is no configured vault"; one means
    "there is one and I cannot see where". Only the last is dangerous, and
    `vault_is_inside_config_dir("")` answered False to all four alike.

    Kept separate from `configured_vault_path` rather than folded into it,
    because that function's contract — "or '' if unreadable" — is what the
    uninstall report relies on to tell the user where their vault still is.
    """
    p = CONFIG_DIR / "config.json"
    if not p.exists():
        return False
    try:
        json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    return False


def _running_from(directory: Path) -> bool:
    """Is the interpreter executing this code installed under `directory`?"""
    try:
        return directory.resolve() in Path(sys.executable).resolve().parents
    except OSError:
        return False


def uninstall(dry_run: bool = False) -> dict:
    """Remove delegation-core's installation, leaving the vault and models.

    What this fixes, which is the reason it exists rather than the shell scripts
    continuing to do the job:

    **Both service registrations are removed, not one.** A full install creates
    two: the MCP daemon (`service.install`, unit `delegation-core`) and the
    llama.cpp autostart (`wizard.py`, unit `delegation-core-llama`). Both
    uninstall scripts removed only the second, because each of the three files
    spelled the unit names out by hand and the daemon's registration arrived
    after they were written. Verified on this machine before the fix:
    `~/.config/systemd/user/delegation-core.service` was present and enabled,
    with `ExecStart=/home/joey/.delegation_core/venv/bin/delegation-core run`
    pointing into the venv the uninstaller deletes, and `Restart=on-failure`.
    So the end state of a "successful" uninstall was a unit that stays enabled,
    fails at every login with 203/EXEC, retries five times per its
    StartLimitBurst, and then sits failed in `systemctl --user status` forever,
    for software the user was told had been removed.

    **The daemon is stopped before anything is deleted.** Neither script stopped
    it. On Linux it kept running out of a deleted venv, still holding the index
    and the port after "Uninstall complete." On Windows the deletions could not
    proceed at all against files a live process holds open, and every one of them
    ended in `>nul 2>&1` with no errorlevel check, so a half-removed install
    reported success.

    The venv is deliberately NOT removed here: this code is running out of it.
    On Windows a running interpreter's own files cannot be deleted, so a version
    of this that tried would half-succeed there and be honest nowhere. The caller
    that launched this process removes it afterwards, and the report says so.
    """
    passos: list[dict] = []

    def _passo(nome: str, ok: bool, **extra) -> dict:
        registro = {"step": nome, "ok": ok, **extra}
        passos.append(registro)
        return registro

    vault = configured_vault_path()
    relatorio: dict = {
        "config_dir": str(CONFIG_DIR),
        "vault_path": vault,
        "kept": {
            "vault": vault or "unknown (config.json missing or unreadable)",
            "models": str(CONFIG_DIR / "models"),
        },
    }

    if not CONFIG_DIR.exists():
        relatorio.update(status="nothing_to_uninstall", steps=passos,
                         detail=f"{CONFIG_DIR} does not exist.")
        return relatorio

    # Refused before the vault check, because it is the case where the vault
    # check has nothing to work with. The report below already prints "unknown
    # (config.json missing or unreadable)" for this state and then removed by
    # exact name anyway — naming the uncertainty in the output and acting on
    # certainty. REMOVE_DIRS starts at "sessions", and the folders the wizard
    # itself suggests are lowercase ["decisions", "research", ..., "sessions"].
    if config_is_unreadable():
        relatorio.update(
            status="refused_config_unreadable",
            steps=passos,
            detail=(
                f"{CONFIG_DIR / 'config.json'} exists but could not be read, so "
                "the configured vault_path is unknown. Everything removed below "
                "is removed by exact name and a vault can hold files by those "
                "same names. Fix or delete config.json, then run this again."
            ),
        )
        return relatorio

    if vault_is_inside_config_dir(vault):
        relatorio.update(
            status="refused_vault_inside_config_dir",
            steps=passos,
            detail=(
                f"The configured vault is {vault}, which is {CONFIG_DIR} or "
                "lives inside it. Everything removed below is removed by exact "
                "name, and a real vault can hold files by those same names. "
                "Move the vault out and update vault_path first."
            ),
        )
        return relatorio

    plano = {
        "dirs": [str(CONFIG_DIR / d) for d in REMOVE_DIRS if (CONFIG_DIR / d).exists()],
        "files": [str(CONFIG_DIR / f) for f in REMOVE_FILES if (CONFIG_DIR / f).exists()],
        "globs": sorted(str(p) for padrao in REMOVE_GLOBS for p in CONFIG_DIR.glob(padrao)),
        "services": [service.SERVICE_NAME, service.LLAMA_SERVICE_NAME],
    }
    relatorio["plan"] = plano

    if dry_run:
        relatorio.update(status="dry_run", steps=passos)
        return relatorio

    # ── stop first, delete second. In that order for a reason. ───────────────
    parada = service.stop()
    _passo("stop_daemon", parada["status"] in ("stopped", "not_installed"), **parada)

    # wait_until_down, not is_up(wait_seconds=15): the latter waits for the port
    # to START answering, so it refused at t=0 on any daemon that was still
    # shutting down and spent the full 15s on every uninstall that had already
    # succeeded. See service.wait_until_down for the timings.
    if not service.wait_until_down(timeout_seconds=15):
        relatorio.update(
            status="refused_daemon_still_up",
            steps=passos,
            detail=(
                "The daemon is still answering on its port 15s after a stop was "
                "requested. Nothing was removed: deleting its files now leaves a "
                "live process holding an index that no longer has a package "
                "behind it. Stop it yourself and run this again."
            ),
        )
        return relatorio

    _passo("unregister_daemon", True, **service.uninstall())
    _passo("unregister_llama_autostart", True, **service.uninstall_llama_autostart())

    # ── removals, each one checked ───────────────────────────────────────────
    removidos: list[str] = []
    falhas: list[dict] = []

    def _remover(caminho: Path) -> None:
        if caminho.name in KEEP_ALWAYS:
            return
        try:
            if caminho.is_dir() and not caminho.is_symlink():
                shutil.rmtree(caminho)
            else:
                caminho.unlink(missing_ok=True)
            removidos.append(str(caminho.relative_to(CONFIG_DIR)))
        except OSError as e:
            falhas.append({"path": str(caminho), "error": str(e)})

    for nome in REMOVE_DIRS:
        alvo = CONFIG_DIR / nome
        if alvo.exists():
            _remover(alvo)
    for nome in REMOVE_FILES:
        alvo = CONFIG_DIR / nome
        if alvo.exists():
            _remover(alvo)
    for padrao in REMOVE_GLOBS:
        for alvo in sorted(CONFIG_DIR.glob(padrao)):
            _remover(alvo)

    _passo("remove_state", not falhas, removed=len(removidos), failures=falhas)

    relatorio.update(
        status="ok" if not falhas else "partial",
        steps=passos,
        removed=sorted(removidos),
        failures=falhas,
    )

    venv = CONFIG_DIR / "venv"
    if venv.exists():
        # `detail` was a constant asserting "this process is running from inside
        # it" while `running_from_it` sat right beside it saying otherwise. The
        # handoff to the wrapper is owed either way — that is what the sentinel
        # below is for — but the two fields must not contradict each other.
        de_dentro = _running_from(venv)
        relatorio["venv_pending"] = {
            "path": str(venv),
            "running_from_it": de_dentro,
            "detail": (
                ("Not removed here: this process is running from inside it. "
                 if de_dentro else
                 "Not removed here: removing a venv is left to the wrapper "
                 "either way, so the step is the same. ")
                + "The wrapper that launched this removes it on the way out."
            ),
        }
        # A sentinel, not a printed line, because the consumer is a shell script
        # that must not have to parse this program's output to know whether the
        # last step is owed. Its absence is what stops the wrapper from deleting
        # a venv after an uninstall that was aborted at the prompt or refused.
        try:
            venv_pending_path().write_text(str(venv) + "\n", encoding="utf-8")
        except OSError as e:
            relatorio["venv_pending"]["sentinel_error"] = str(e)
    return relatorio


# ── post-install: everything install.sh/.bat did after `pip install` ─────────

DEFAULT_REPO_SLUG = "AnonJoey/delegation-core-v7"


def repo_slug(root: Path) -> str:
    """The GitHub `owner/name` to pull dashboard releases from.

    `origin` is the repo that publishes releases. `fork` is only consulted for
    checkouts predating the 2026-08-31 remote rename, where the active repo was
    named `fork` and `origin` pointed at the frozen v6.4: asking the wrong one
    makes the release download look in a repo with no releases.
    """
    for remote in ("origin", "fork"):
        codigo, url = _git(root, "remote", "get-url", remote)
        if codigo != 0:
            continue
        limpo = url.strip().removesuffix(".git")
        for prefixo in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
            if limpo.startswith(prefixo):
                caminho = limpo[len(prefixo):]
                if caminho.count("/") == 1:
                    return caminho
    return DEFAULT_REPO_SLUG


def install_skills(root: Path) -> dict:
    """Copy bundled Claude skills into ~/.claude/skills, never clobbering.

    Personal skills there are available in every Claude Code session on the
    machine, independent of any plugin config. A skill the user already has by
    that name is theirs: kept, and reported as kept.
    """
    origem = root / "skills"
    resultado: dict = {"installed": [], "kept_yours": [], "available": bool(origem.is_dir())}
    if not origem.is_dir():
        return resultado

    destino_raiz = Path.home() / ".claude" / "skills"
    destino_raiz.mkdir(parents=True, exist_ok=True)
    for pasta in sorted(p for p in origem.iterdir() if p.is_dir()):
        destino = destino_raiz / pasta.name
        if destino.exists():
            resultado["kept_yours"].append(pasta.name)
            continue
        try:
            shutil.copytree(pasta, destino)
            resultado["installed"].append(pasta.name)
        except OSError as e:
            resultado.setdefault("errors", []).append({"skill": pasta.name, "error": str(e)})
    return resultado


def install_agents(root: Path) -> dict:
    """Copy bundled agent definitions into ~/.claude/agents, never clobbering.

    Same contract as `install_skills`, one directory over: agents are single
    markdown files rather than folders. An agent the user already has by that
    name is theirs, kept and reported as kept, because a subagent someone tuned
    for their own workflow must not be silently replaced by a shipped one.
    """
    origem = root / "agents"
    resultado: dict = {"installed": [], "kept_yours": [], "available": bool(origem.is_dir())}
    if not origem.is_dir():
        return resultado

    destino_raiz = Path.home() / ".claude" / "agents"
    destino_raiz.mkdir(parents=True, exist_ok=True)
    for arquivo in sorted(origem.glob("*.md")):
        destino = destino_raiz / arquivo.name
        if destino.exists():
            resultado["kept_yours"].append(arquivo.stem)
            continue
        try:
            shutil.copy2(arquivo, destino)
            resultado["installed"].append(arquivo.stem)
        except OSError as e:
            resultado.setdefault("errors", []).append({"agent": arquivo.stem, "error": str(e)})
    return resultado


def _dashboard_artifact(root: Path, subdir: str, sufixo: str) -> Path | None:
    pasta = root / "dashboard" / "src-tauri" / "target" / "release" / "bundle" / subdir
    if not pasta.is_dir():
        return None
    achados = sorted(pasta.glob(f"*{sufixo}"))
    return achados[0] if achados else None


def _download_release_asset(root: Path, padrao: str, destino: Path) -> Path | None:
    """Fetch one asset from the latest GitHub release via `gh`, or None.

    Best-effort throughout: `gh` absent, not logged in, no release, no matching
    asset are all "no dashboard", never a failed install. The MCP side is what
    matters, and it is already in place by the time this runs.
    """
    if shutil.which("gh") is None:
        return None
    destino.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["gh", "release", "download", "--repo", repo_slug(root),
             "--pattern", padrao, "--dir", str(destino)],
            capture_output=True, text=True, timeout=600,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    achados = sorted(destino.glob(padrao))
    return achados[0] if achados else None


def _desktop_entry_text(exec_path: Path, icone: str) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=delegation-core Dashboard\n"
        f"Exec={exec_path}\n"
        f"Icon={icone}\n"
        "Categories=Utility;\n"
    )


def _install_appimage(root: Path, appimage: Path) -> dict:
    """Place the AppImage and give it a real menu entry with a real icon.

    The icon goes into the XDG hicolor theme so the .desktop file can name it
    and every desktop environment resolves it. Falls back to a stock icon name
    when the source assets are not in this checkout, which is the case for a
    stripped-down distribution.
    """
    destino_dir = CONFIG_DIR / "app"
    destino_dir.mkdir(parents=True, exist_ok=True)
    destino = destino_dir / "delegation-core-dashboard.AppImage"
    shutil.copyfile(appimage, destino)
    destino.chmod(0o755)

    icones = root / "dashboard" / "src-tauri" / "icons"
    nome_icone = "utilities-terminal"
    if (icones / "128x128.png").is_file():
        for origem, tamanho in ((icones / "128x128.png", "128x128"),
                                (icones / "32x32.png", "32x32"),
                                (icones / "128x128@2x.png", "256x256@2")):
            if not origem.is_file():
                continue
            alvo_dir = Path.home() / ".local" / "share" / "icons" / "hicolor" / tamanho / "apps"
            alvo_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(origem, alvo_dir / "delegation-core-dashboard.png")
        nome_icone = "delegation-core-dashboard"

    apps = Path.home() / ".local" / "share" / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    (apps / "delegation-core-dashboard.desktop").write_text(
        _desktop_entry_text(destino, nome_icone), encoding="utf-8")
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", "-q", str(apps)],
                       capture_output=True, timeout=60)
    return {"method": "appimage", "status": "installed", "path": str(destino)}


def install_dashboard(root: Path) -> dict:
    """Install the Tauri dashboard, preferring a local build over a release.

    Never fails the install. Every branch that cannot proceed returns a status
    the caller prints as a note, because the Python/MCP side is the part that
    matters and a missing dashboard is a missing convenience.
    """
    import platform as _platform
    import tempfile

    sistema = _platform.system()
    manual = ("Build it manually: cd dashboard && npm install && npm run tauri build")

    try:
        if sistema == "Linux":
            deb = _dashboard_artifact(root, "deb", ".deb")
            rpm = _dashboard_artifact(root, "rpm", ".rpm")
            appimage = _dashboard_artifact(root, "appimage", ".AppImage")

            if not (deb or rpm or appimage):
                tmp = Path(tempfile.mkdtemp(prefix="dc-dashboard-"))
                if shutil.which("dpkg"):
                    deb = _download_release_asset(root, "*.deb", tmp)
                elif shutil.which("rpm"):
                    rpm = _download_release_asset(root, "*.rpm", tmp)
                else:
                    appimage = _download_release_asset(root, "*.AppImage", tmp)

            if deb and shutil.which("dpkg"):
                p = subprocess.run(["sudo", "dpkg", "-i", str(deb)],
                                   capture_output=True, text=True, timeout=600)
                if p.returncode != 0 and shutil.which("apt-get"):
                    p = subprocess.run(["sudo", "apt-get", "install", "-f", "-y"],
                                       capture_output=True, text=True, timeout=600)
                return {"method": "deb", "status": "installed" if p.returncode == 0 else "failed",
                        "path": str(deb), "detail": (p.stdout + p.stderr).strip()[-400:]}
            if rpm and shutil.which("rpm"):
                p = subprocess.run(["sudo", "rpm", "-i", str(rpm)],
                                   capture_output=True, text=True, timeout=600)
                if p.returncode != 0 and shutil.which("dnf"):
                    p = subprocess.run(["sudo", "dnf", "install", "-y", str(rpm)],
                                       capture_output=True, text=True, timeout=600)
                return {"method": "rpm", "status": "installed" if p.returncode == 0 else "failed",
                        "path": str(rpm), "detail": (p.stdout + p.stderr).strip()[-400:]}
            if appimage:
                return _install_appimage(root, appimage)
            return {"method": None, "status": "not_found", "detail": manual}

        if sistema == "Darwin":
            dmg = _dashboard_artifact(root, "dmg", ".dmg")
            if dmg is None:
                dmg = _download_release_asset(root, "*.dmg", Path(tempfile.mkdtemp(prefix="dc-dashboard-")))
            if dmg is None:
                return {"method": None, "status": "not_found", "detail": manual}

            p = subprocess.run(["hdiutil", "attach", str(dmg), "-nobrowse", "-readonly"],
                               capture_output=True, text=True, timeout=300)
            # `hdiutil attach` exit status is what decides, not the last word of
            # its output. The shell version piped it and lost the status, so a
            # garbled mount left an empty path and `"$mnt"/*.app` collapsed to
            # `/*.app`, matching unrelated bundles at the filesystem root and
            # copying them into /Applications.
            montagem = None
            if p.returncode == 0 and p.stdout.strip():
                ultima = p.stdout.strip().splitlines()[-1].split("\t")[-1].strip()
                if ultima.startswith("/") and Path(ultima).is_dir():
                    montagem = Path(ultima)
            if montagem is None:
                return {"method": "dmg", "status": "failed",
                        "detail": f"could not mount {dmg.name}: {(p.stdout + p.stderr).strip()[-300:]}"}
            try:
                apps = sorted(montagem.glob("*.app"))
                if not apps:
                    return {"method": "dmg", "status": "not_found", "detail": manual}
                alvo = Path("/Applications") / apps[0].name
                if alvo.exists():
                    shutil.rmtree(alvo)
                shutil.copytree(apps[0], alvo)
                return {"method": "dmg", "status": "installed", "path": str(alvo)}
            finally:
                subprocess.run(["hdiutil", "detach", str(montagem), "-quiet"],
                               capture_output=True, timeout=120)

        if sistema == "Windows":
            msi = _dashboard_artifact(root, "msi", ".msi")
            nsis = _dashboard_artifact(root, "nsis", ".exe")
            if not (msi or nsis):
                tmp = Path(tempfile.mkdtemp(prefix="dc-dashboard-"))
                msi = _download_release_asset(root, "*.msi", tmp) or None
                if msi is None:
                    nsis = _download_release_asset(root, "*.exe", tmp)
            if msi:
                p = subprocess.run(["msiexec", "/i", str(msi), "/qb"],
                                   capture_output=True, text=True, timeout=1800)
                return {"method": "msi", "status": "installed" if p.returncode == 0 else "failed",
                        "path": str(msi), "detail": (p.stdout + p.stderr).strip()[-400:]}
            if nsis:
                p = subprocess.run([str(nsis), "/S"], capture_output=True, text=True, timeout=1800)
                return {"method": "nsis", "status": "installed" if p.returncode == 0 else "failed",
                        "path": str(nsis), "detail": (p.stdout + p.stderr).strip()[-400:]}
            return {"method": None, "status": "not_found", "detail": manual}

    except (OSError, subprocess.SubprocessError) as e:
        return {"method": None, "status": "failed", "detail": str(e)}

    return {"method": None, "status": "unsupported", "detail": f"no dashboard bundle for {sistema}"}


def repair_client_configs() -> dict:
    """Refresh the MCP client entries that an upgrade must not leave stale.

    Two things this fixes, both of them divergences rather than oversights:

    `install.bat` ran `clients --claude-code` on an upgrade and `install.sh` did
    not, so the same upgrade refreshed the client config on Windows and left it
    untouched on Linux and macOS.

    Neither ran `--claude-desktop`, which matters more since the Desktop entry
    turned out to be actively destructive: written in the Claude Code shape it
    carries a `url`, and Desktop responds to that by rewriting the file and
    dropping the WHOLE `mcpServers` section. Someone upgrading to the code that
    fixes this would still be carrying the entry that causes it.

    Only ever a REPAIR for Desktop: an entry is rewritten when one already
    exists, and no entry is created for someone who never configured Desktop.
    An installer that adds itself to a client the user did not ask for is a
    different thing from one that fixes what it previously wrote wrong.
    """
    from .config import Config

    resultado: dict = {}
    try:
        cfg = Config.load()
    except Exception as e:
        return {"status": "skipped", "detail": f"config not loadable: {e}"}

    from . import clients

    try:
        resultado["claude_code"] = clients.install_claude_code(cfg)
    except Exception as e:
        resultado["claude_code"] = {"status": "error", "detail": str(e)}

    alvo = clients.claude_desktop_config_path()
    ja_configurado = False
    if alvo.is_file():
        try:
            dados = json.loads(alvo.read_text(encoding="utf-8"))
            ja_configurado = "delegation-core" in (dados.get("mcpServers") or {})
        except (OSError, json.JSONDecodeError):
            ja_configurado = False

    if ja_configurado:
        try:
            resultado["claude_desktop"] = clients.install_claude_desktop(cfg)
        except Exception as e:
            resultado["claude_desktop"] = {"status": "error", "detail": str(e)}
    else:
        resultado["claude_desktop"] = {
            "status": "not_configured",
            "detail": ("No delegation-core entry in Claude Desktop's config, so "
                       "none was written. Run `delegation-core clients "
                       "--claude-desktop` to add one."),
        }
    return resultado


def post_install(root: Path) -> dict:
    """Everything install.sh and install.bat did after `pip install`.

    Shared by the three platforms instead of transcribed into each. Whether the
    setup wizard should run is reported, not decided: the shell wrapper is what
    can hand the terminal over to an interactive program.
    """
    relatorio: dict = {"root": str(root)}

    relatorio["docs_and_hooks"] = refresh_shipped_files(root)
    relatorio["skills"] = install_skills(root)
    relatorio["agents"] = install_agents(root)
    relatorio["dashboard"] = install_dashboard(root)

    # The cached health file predates the recursive broken-link metric. Dropping
    # it makes the next start recompute rather than serve a stale count.
    try:
        from .config import vault_health_cache
        vault_health_cache().unlink(missing_ok=True)
        relatorio["health_cache"] = "invalidated"
    except OSError as e:
        relatorio["health_cache"] = f"could not remove: {e}"

    fresca = not (CONFIG_DIR / "config.json").is_file()
    relatorio["fresh_install"] = fresca

    if fresca:
        # Nothing to register or repair yet: the wizard writes the config that
        # both of those read.
        relatorio["needs_wizard"] = True
        return relatorio

    relatorio["needs_wizard"] = False
    relatorio["service"] = service.install()
    relatorio["clients"] = repair_client_configs()
    relatorio["stale_dist_copies"] = stale_dist_copies()
    return relatorio
