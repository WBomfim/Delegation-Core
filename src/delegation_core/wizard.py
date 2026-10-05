"""
wizard.py: Interactive setup wizard for delegation-core.
Designed for non-technical users: numbered menus, progress bars, clear prompts.
No technical knowledge required.
"""

import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from .config import Config, CONFIG_DIR
from .downloader import MODELS, download_llama_binary, download_model, find_llama_binary

console = Console()


# ── entry point ───────────────────────────────────────────────────────────────

def run_wizard():
    _welcome()
    try:
        cfg = Config.load()

        _header("System Check", "Detecting your environment")
        _step_system_check()

        _header("Step 1 of 7", "Your Obsidian Vault")
        vault_path, vault_folders = _step_vault()
        cfg.vault_path = str(vault_path)
        cfg.vault_folders = vault_folders

        _header("Step 2 of 7", "AI Engine")
        cfg.engine_mode = _step_engine_mode()

        if cfg.engine_mode == "agent":
            # Agent mode: no local model: nothing to download. Generation is
            # delegated to the calling Claude; embeddings/search stay local.
            cfg.llama_model = ""
            cfg.llama_binary = ""
            console.print("  [green]✓[/green] Agent mode: skipping model and engine "
                          "download. Claude will handle generation.\n")
        else:
            _header("Step 3 of 7", "AI Model")
            cfg.llama_model = _step_model(cfg.models_dir)

            _header("Step 3b of 7", "Local Engine (llama.cpp)")
            cfg.llama_binary = _step_binary(cfg.llama_dir)

        _header("Step 4 of 7", "Background Startup")
        auto_start = _step_startup()

        _header("Step 5 of 7", "v0.2 Features")
        synthesis_enabled, synthesis_lang, budget_mode = _step_features()
        cfg.synthesis_enabled = synthesis_enabled
        cfg.synthesis_lang    = synthesis_lang
        cfg.budget_mode       = budget_mode

        cfg.save()

        _header("Step 6 of 7", "Embedding Model")
        _step_bge(cfg.bge_model)

        _header("Step 7 of 7", "Building Search Index")
        _step_index(cfg)

        if auto_start:
            _setup_startup(cfg)

        _completion(cfg)

    except KeyboardInterrupt:
        console.print("\n\n[yellow]Setup cancelled.[/yellow]\n")
        sys.exit(0)


# ── system check ─────────────────────────────────────────────────────────────

def _step_system_check():
    system = platform.system()
    machine = platform.machine()
    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    ok  = "[green]✓[/green]"
    bad = "[red]✗[/red]"

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("label", style="dim", min_width=22)
    table.add_column("value")

    os_names = {"Linux": "Linux", "Darwin": "macOS", "Windows": "Windows"}
    table.add_row("Operating System", f"{os_names.get(system, system)}  ({machine})")
    table.add_row("Python", py)

    internet = _check_internet()
    table.add_row("Internet", f"{ok} connected" if internet else f"{bad} [red]no connection[/red]")

    if system == "Linux":
        pkg_status, missing = _check_linux_packages()
        table.add_row("System packages", pkg_status)
    elif system == "Darwin":
        xcode_ok = _check_xcode()
        table.add_row("Xcode CLT", f"{ok} installed" if xcode_ok else "[yellow]⚠[/yellow]  not found")

    console.print(table)
    console.print()

    if not internet:
        console.print("  [red]An internet connection is required to download the AI model and engine.[/red]")
        console.print("  Connect to the internet and run setup again.\n")
        sys.exit(1)

    if system == "Linux" and missing:
        _install_linux_packages(missing)
    elif system == "Darwin" and not _check_xcode():
        _install_xcode()

    # Final Python package availability check
    _verify_python_packages()

    console.print(f"  {ok} Environment ready.\n")


def _check_internet() -> bool:
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=4)
        return True
    except OSError:
        return False


def _check_linux_packages() -> tuple[str, list[str]]:
    if not shutil.which("dpkg"):
        return "[dim]non-apt system: skipped[/dim]", []
    required = ["python3-venv", "python3-dev", "build-essential"]
    missing = []
    for pkg in required:
        r = subprocess.run(["dpkg", "-s", pkg], capture_output=True, text=True)
        if r.returncode != 0:
            missing.append(pkg)
    if missing:
        return f"[yellow]missing: {', '.join(missing)}[/yellow]", missing
    return "[green]✓[/green]  all present", []


def _install_linux_packages(missing: list[str]):
    console.print(f"  Missing system packages: [bold]{', '.join(missing)}[/bold]")
    raw = console.input("  Install them now? (requires sudo password) [Y/n]: ").strip().lower()
    if raw in ("n", "no"):
        console.print("  [yellow]Skipped.[/yellow] You may encounter errors during installation.\n")
        return
    console.print()
    try:
        subprocess.run(["sudo", "apt-get", "install", "-y"] + missing, check=True)
        console.print("\n  [green]✓[/green] System packages installed.\n")
    except subprocess.CalledProcessError:
        console.print("\n  [red]Install failed.[/red] Try manually:")
        console.print(f"    sudo apt-get install {' '.join(missing)}\n")


def _check_xcode() -> bool:
    r = subprocess.run(["xcode-select", "-p"], capture_output=True, text=True)
    return r.returncode == 0


def _install_xcode():
    console.print("  [yellow]Xcode Command Line Tools are needed to install Python packages.[/yellow]")
    raw = console.input("  Install them now? [Y/n]: ").strip().lower()
    if raw in ("n", "no"):
        console.print("  [yellow]Skipped.[/yellow] You may see errors during installation.\n")
        return
    console.print("  Starting Xcode installer: a dialog will appear, click Install.")
    subprocess.run(["xcode-select", "--install"], capture_output=True)
    console.input("  Press Enter once the Xcode installer has finished: ")
    console.print()


def _verify_python_packages():
    checks = [
        ("fastmcp",             "fastmcp"),
        ("chromadb",            "chromadb"),
        ("sentence_transformers","sentence-transformers"),
        ("requests",            "requests"),
        ("rich",                "rich"),
    ]
    missing = []
    for module, pkg in checks:
        try:
            __import__(module)
        except ImportError:
            missing.append(pkg)
    if missing:
        console.print(f"  [red]Missing Python packages:[/red] {', '.join(missing)}")
        console.print("  Run [bold]pip install -e .[/bold] first.\n")
        sys.exit(1)


# ── steps ─────────────────────────────────────────────────────────────────────

def _step_vault() -> tuple[Path, list[str]]:
    defaults = ["decisions", "research", "tools", "fixes", "reference", "sessions"]
    console.print("  Where is your Obsidian vault?\n")
    console.print("  [dim]delegation-core will read and write markdown notes here.[/dim]\n")

    while True:
        raw = console.input("  Vault path (e.g. ~/Documents/Vault): ").strip()
        if not raw:
            continue
        vault_path = Path(raw).expanduser().resolve()
        if _conflicts_with_config_dir(vault_path):
            _warn_config_dir_conflict(vault_path)
            continue
        if vault_path.exists() and not vault_path.is_dir():
            console.print("  [red]That path is a file, not a directory. Try again.[/red]\n")
            continue
        break

    if not vault_path.exists():
        console.print(f"\n  Directory [bold]{vault_path}[/bold] does not exist.")
        raw = console.input("  Create it now? [Y/n]: ").strip().lower()
        if raw not in ("", "y", "yes"):
            console.print("\n  [yellow]Setup cancelled.[/yellow]\n")
            sys.exit(0)
        vault_path.mkdir(parents=True, exist_ok=True)
        console.print(f"  [green]✓[/green] Created {vault_path}")

    existing_dirs = [d.name for d in vault_path.iterdir() if d.is_dir() and not d.name.startswith(".")]
    if existing_dirs:
        console.print(f"\n  Found existing folders in vault: [bold]{', '.join(existing_dirs)}[/bold]")
        raw = console.input("  Use these folders for the vault structure? [Y/n]: ").strip().lower()
        if raw in ("", "y", "yes"):
            folders = existing_dirs
        else:
            folders = defaults
            for f in defaults:
                (vault_path / f).mkdir(exist_ok=True)
            console.print(f"  [green]Created default folders:[/green] {', '.join(defaults)}")
    else:
        console.print(f"\n  Creating standard vault folders: [bold]{', '.join(defaults)}[/bold]")
        for f in defaults:
            (vault_path / f).mkdir(exist_ok=True)
        folders = defaults
        console.print(f"  [green]Created:[/green] {', '.join(defaults)}")

    console.print(f"\n  [green]✓[/green] Vault ready: {vault_path}\n")
    return vault_path, folders


def _conflicts_with_config_dir(path: Path) -> bool:
    """True when `path` must not be used as a vault.

    uninstall.sh / uninstall.bat remove a fixed list of CONFIG_DIR subpaths by
    name (sessions/, config.json, graphs/, ...) without ever touching the
    configured vault_path directly. That guarantee only holds if the vault
    itself is never placed at/under CONFIG_DIR in the first place: otherwise
    a targeted removal could coincidentally delete real vault content that
    happens to share one of those names — and `Sessions` is a vault folder on
    every install, against the uninstaller's `sessions/`. Reject the path here
    instead.

    A path that cannot be resolved is rejected too. This used to `return False`
    — "no conflict" — for a path it had failed to examine, which is a check that
    passes when it cannot check. `_unindexed_notes` already carries the rule in
    its own docstring: degrade to "cannot tell", never to "all fine". The cost
    of being wrong in this direction is the user picking a different folder; the
    cost in the other direction is the uninstaller deleting their notes.
    """
    try:
        resolved = path.resolve()
        cfg = CONFIG_DIR.resolve()
    except OSError:
        return True
    return resolved == cfg or cfg in resolved.parents


def _warn_config_dir_conflict(path: Path):
    # Two reasons reach here — it really is under CONFIG_DIR, or it could not be
    # resolved at all — and the message used to assert the first for both.
    console.print(f"  [red]That path cannot be used as a vault: it is "
                  f"delegation-core's own config directory ({CONFIG_DIR}), is "
                  f"inside it, or could not be resolved.[/red]")
    console.print("  [red]Choose a location outside of it: uninstalling could "
                  "otherwise delete vault content.[/red]\n")


def _step_engine_mode() -> str:
    """Choose where generation runs: local model, the calling Claude, or hybrid.

    Returns "local", "agent", or "hybrid". Embeddings + search always run locally.
    """
    console.print("  How should delegation-core generate summaries and compress content?\n")
    console.print("  [bold]1. Local model[/bold] (llama.cpp)")
    console.print("     [dim]Runs a model on this machine. Fully offline, but uses RAM/CPU[/dim]")
    console.print("     [dim]and competes with your other apps. Downloads ~2 GB on setup.[/dim]\n")
    console.print("  [bold]2. Agent (Claude does it)[/bold]  [green](lightest)[/green]")
    console.print("     [dim]No local model. The Claude you're talking to handles generation;[/dim]")
    console.print("     [dim]this machine only runs the (small) embedding + search layer. Nothing[/dim]")
    console.print("     [dim]to download. Best if the local model strains your hardware.[/dim]\n")
    console.print("  [bold]3. Hybrid[/bold]  [green](recommended)[/green]")
    console.print("     [dim]Interactive work goes to Claude (fast, no load); big/slow/bulk[/dim]")
    console.print("     [dim]jobs (whole-vault synthesis, healing) use the local model in the[/dim]")
    console.print("     [dim]background. Oversized calls show their token cost and let you choose[/dim]")
    console.print("     [dim]local vs Claude. Downloads ~2 GB (needs the local model on hand).[/dim]\n")

    choice = _menu_index("Choose an engine", 3)
    mode = {0: "local", 1: "agent", 2: "hybrid"}[choice]
    label = {"local": "Local model (llama.cpp)",
             "agent": "Agent: Claude handles generation",
             "hybrid": "Hybrid: Claude for interactive, local model for big/bulk"}[mode]
    console.print(f"\n  [green]✓[/green] Engine: {label}\n")
    return mode


def _step_model(models_dir: Path) -> str:
    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("#", style="bold cyan", width=3)
    table.add_column("Model", min_width=16)
    table.add_column("Download", min_width=9)
    table.add_column("RAM needed", min_width=10)
    table.add_column("Description")

    for i, m in enumerate(MODELS, 1):
        name = m["name"]
        if m.get("recommended"):
            name += "  [yellow]★ recommended[/yellow]"
        already = (models_dir / m["filename"]).exists()
        size_str = "[green]on disk[/green]" if already else m["size"]
        table.add_row(str(i), name, size_str, m["ram"], m["description"])

    console.print("  These models run entirely on your computer.\n")
    console.print(table)
    console.print()

    choice = _menu_index("Choose a model", len(MODELS))
    model = MODELS[choice]

    dest = models_dir / model["filename"]
    if dest.exists():
        console.print(f"\n  [green]✓[/green] Already downloaded: {model['name']}\n")
    else:
        console.print(f"\n  Downloading [bold]{model['name']}[/bold] ({model['size']}): this may take a few minutes...\n")
        result = download_model(model, models_dir)
        if not result:
            console.print("\n  [red]Download failed.[/red] Check your internet connection and run setup again.")
            sys.exit(1)
        console.print(f"\n  [green]✓[/green] {model['name']} ready.\n")

    return str(dest)


def _step_binary(llama_dir: Path) -> str:
    existing = find_llama_binary(llama_dir)

    if existing:
        console.print(f"  Found existing llama.cpp: [bold]{existing}[/bold]")
        raw = console.input("  Use this? [Y/n]: ").strip().lower()
        if raw in ("", "y", "yes"):
            console.print("\n  [green]✓[/green] Using existing binary.\n")
            return str(existing)

    console.print("  llama.cpp is the engine that runs the AI model locally.")
    console.print("  We can download and install it automatically.\n")
    raw = console.input("  Download llama.cpp automatically? [Y/n]: ").strip().lower()

    if raw in ("n", "no"):
        raw = console.input("  Enter the full path to your llama-server binary: ").strip()
        return str(Path(raw).expanduser())

    console.print()
    result = download_llama_binary(llama_dir)
    if result:
        console.print("\n  [green]✓[/green] llama.cpp installed.\n")
        return str(result)

    console.print("\n  [yellow]Automatic download failed.[/yellow]")
    console.print("  You can download it manually from:")
    console.print("  [bold]https://github.com/ggml-org/llama.cpp/releases[/bold]\n")
    raw = console.input("  Enter path to llama-server binary once downloaded: ").strip()
    return str(Path(raw).expanduser())


def _step_startup() -> bool:
    system = platform.system()
    methods = {"Linux": "systemd user service", "Darwin": "launchd agent", "Windows": "Task Scheduler"}
    method = methods.get(system, "background service")

    console.print("  Should the AI engine start automatically when you log in?")
    console.print(f"  [dim]Method: {method}[/dim]")
    console.print("  [dim]Without this, there's a ~30 second warmup on first use each session.[/dim]\n")
    raw = console.input("  Auto-start at login? [Y/n]: ").strip().lower()
    result = raw in ("", "y", "yes")
    console.print()
    return result


def _step_features() -> tuple[bool, str, str]:
    """Ask about v0.2 config: synthesis, language, budget mode."""
    console.print("  Configure the v0.2 processing features.\n")

    # Synthesis
    console.print("  [bold]Note synthesis[/bold]")
    console.print("  When enabled, inbox files are converted into structured Obsidian notes")
    console.print("  by the local AI model (sections, frontmatter, bullet points).")
    console.print("  Disable to file raw text directly: faster but less organised.\n")
    raw = console.input("  Enable note synthesis? [Y/n]: ").strip().lower()
    synthesis_enabled = raw not in ("n", "no")
    console.print()

    # Language
    synthesis_lang = "en"
    if synthesis_enabled:
        console.print("  [bold]Synthesis language[/bold]")
        lang_choice = _menu("Choose the language for synthesised notes:", [
            "English (default)",
            "Portuguese (Brazilian)",
        ])
        synthesis_lang = "pt" if lang_choice == 1 else "en"
        console.print()

    # Budget mode
    console.print("  [bold]Budget mode[/bold]")
    console.print("  CPU mode applies strict per-task token caps to stay within MCP timeouts")
    console.print("  on low-power machines (e.g. i9 Mac without GPU offload).")
    console.print("  [dim]normal[/dim] : full-quality outputs (recommended on any GPU machine)")
    console.print("  [dim]cpu[/dim]    : hard caps: classify=8, compress=200, synthesize=2500\n")
    budget_choice = _menu("Select budget mode:", [
        "normal : full quality  [dim](recommended)[/dim]",
        "cpu : strict token caps for low-power machines",
    ])
    budget_mode = "cpu" if budget_choice == 1 else "normal"

    console.print(
        f"\n  [green]✓[/green]  synthesis={'on' if synthesis_enabled else 'off'} ({synthesis_lang})  "
        f"budget={budget_mode}\n"
    )
    return synthesis_enabled, synthesis_lang, budget_mode


def _step_bge(model_name: str):
    console.print(f"  Downloading the search embedding model [bold]{model_name}[/bold].")
    console.print("  [dim]~110 MB (one-time download. Runs locally forever after).[/dim]\n")
    try:
        from sentence_transformers import SentenceTransformer
        SentenceTransformer(model_name)
        console.print("  [green]✓[/green] Embedding model ready.\n")
    except Exception as e:
        console.print(f"  [yellow]Warning:[/yellow] {e}")
        console.print("  It will download automatically on first use.\n")


def _step_index(cfg: Config):
    console.print(f"  Indexing notes in [bold]{cfg.vault_path}[/bold]...")
    try:
        from .vault import VaultManager
        vault = VaultManager(cfg)
        count = vault.reindex_vault()
        console.print(f"  [green]✓[/green] {count} notes indexed and searchable.\n")
    except Exception as e:
        console.print(f"  [yellow]Warning:[/yellow] Could not build index: {e}")
        console.print("  Run [bold]delegation-core reindex[/bold] after setup to fix this.\n")


# ── startup configuration ─────────────────────────────────────────────────────

def _setup_startup(cfg: Config):
    if cfg.is_agent_mode or not cfg.llama_binary or not cfg.llama_model:
        console.print("  [dim]Agent mode (no local model): skipping background engine service.[/dim]\n")
        return
    system = platform.system()
    console.print("  Configuring background startup...")
    try:
        if system == "Linux":
            _startup_systemd(cfg)
        elif system == "Darwin":
            _startup_launchd(cfg)
        elif system == "Windows":
            _startup_task_scheduler(cfg)
        console.print("  [green]✓[/green] AI engine will start automatically at login.\n")
    except Exception as e:
        console.print(f"  [yellow]Warning:[/yellow] Could not configure auto-start: {e}\n")


def _systemd_literal(value) -> str:
    """A user-controlled path as a systemd unit file reads it, not as we meant it.

    `%` opens a specifier in a unit file, and the escape for a literal one is
    `%%`. The paths interpolated below are chosen by the person running setup,
    and `%` is a perfectly ordinary character in a directory name.

    Measured on 2026-09-03, with the unit actually loaded and
    `systemctl show -p ExecStart` asked what it understood::

        written : ExecStart="/home/joey/100%hits/llama-server"
                    --model "/data/%name%.gguf"
        loaded  : path=/home/joey/100/home/joeyits/llama-server
                  argv[]=... --model /data/<unit name>ame%.gguf

    `%h` became the home directory and `%n` the unit name. The service then
    points at a path that does not exist, the engine never starts at login, and
    setup has already printed "AI engine will start automatically at login".

    The quoting one line down was added for the same reason — a space in any of
    these paths splits the command — so the hostility of these values was
    already known here. This is the second metacharacter, not a new idea.
    """
    return str(value).replace("%", "%%")


def _startup_systemd(cfg: Config):
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)

    service = (
        "[Unit]\n"
        "Description=llama.cpp server for delegation-core\n"
        "After=graphical-session.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        # ExecStart= uses systemd's own shell-like word splitting on
        # whitespace, paths must be quoted or a space anywhere in the home
        # directory, models dir, or binary path (all user-controlled) splits
        # into the wrong number of arguments. `%` needs _systemd_literal for
        # the same reason, one layer down: see its docstring.
        f'ExecStart="{_systemd_literal(cfg.llama_binary)}"'
        f' --model "{_systemd_literal(cfg.llama_model)}"'
        f" --port {cfg.llama_port}"
        f" --ctx-size {cfg.llama_ctx}"
        f" --n-gpu-layers {cfg.llama_ngl}\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        f"StandardOutput=append:{_systemd_literal(cfg.llama_log_path)}\n"
        f"StandardError=append:{_systemd_literal(cfg.llama_log_path)}\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )

    # Name and path both come from service.py, which is where the uninstaller
    # reads them too. They used to be spelled out independently in five files.
    from . import service as _svc
    service_file = _svc.LLAMA_SYSTEMD_UNIT
    # encoding explicito, como service.py ja faz para a unit DELE. Sem ele
    # o Python usa locale.getpreferredencoding(): medido sob LC_ALL=C, um
    # caminho com acento levanta UnicodeEncodeError e o usuario recebe
    # "Could not configure auto-start" em vez do motor no login.
    service_file.write_text(service, encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", _svc.LLAMA_SERVICE_NAME], check=True)


def _startup_launchd(cfg: Config):
    from . import service as _svc
    _LLAMA_LABEL = _svc.LLAMA_LAUNCHD_LABEL
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    # Um `&`, `<` ou `>` num caminho e legal no macOS e ILEGAL cru em XML: sem
    # escape o plist sai malformado, `launchctl load` recusa, e o motor nunca
    # sobe. Medido em 03/09/2026 com "/Users/joey/Documents/AI & ML/llama-server":
    # "not well-formed (invalid token): line 5, column 38".
    # E o mesmo problema do `%` do systemd na funcao acima, no formato vizinho:
    # valor escolhido pelo usuario interpolado cru num formato que da significado
    # a alguns caracteres.
    esc = xml_escape

    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
        ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        f'  <key>Label</key><string>{esc(_LLAMA_LABEL)}</string>\n'
        '  <key>ProgramArguments</key>\n'
        '  <array>\n'
        f'    <string>{esc(str(cfg.llama_binary))}</string>\n'
        f'    <string>--model</string><string>{esc(str(cfg.llama_model))}</string>\n'
        f'    <string>--port</string><string>{cfg.llama_port}</string>\n'
        f'    <string>--ctx-size</string><string>{cfg.llama_ctx}</string>\n'
        f'    <string>--n-gpu-layers</string><string>{cfg.llama_ngl}</string>\n'
        '  </array>\n'
        '  <key>RunAtLoad</key><true/>\n'
        '  <key>KeepAlive</key><true/>\n'
        f'  <key>StandardOutPath</key><string>{esc(str(cfg.llama_log_path))}</string>\n'
        f'  <key>StandardErrorPath</key><string>{esc(str(cfg.llama_log_path))}</string>\n'
        '</dict></plist>\n'
    )

    plist_file = _svc.LLAMA_LAUNCHD_PLIST
    plist_file.write_text(plist, encoding="utf-8")
    subprocess.run(["launchctl", "load", str(plist_file)], check=True)


def _startup_task_scheduler(cfg: Config):
    from . import service as _svc
    cmd_str = (
        f'"{cfg.llama_binary}" --model "{cfg.llama_model}"'
        f' --port {cfg.llama_port} --ctx-size {cfg.llama_ctx}'
        f' --n-gpu-layers {cfg.llama_ngl}'
    )
    subprocess.run(
        ["schtasks", "/create",
         "/tn", _svc.LLAMA_SERVICE_NAME,
         "/tr", cmd_str,
         "/sc", "ONLOGON",
         "/rl", "HIGHEST",
         "/f"],
        check=True, capture_output=True,
    )
    subprocess.run(["schtasks", "/run", "/tn", _svc.LLAMA_SERVICE_NAME], capture_output=True)


# ── completion ────────────────────────────────────────────────────────────────

def _completion(cfg: Config):
    system = platform.system()
    venv_bin = Path(sys.executable).parent
    if system == "Windows":
        exe = str(venv_bin / "delegation-core.exe")
        desktop_config_path = "%APPDATA%\\Claude\\claude_desktop_config.json"
    elif system == "Darwin":
        exe = str(venv_bin / "delegation-core")
        desktop_config_path = "~/Library/Application Support/Claude/claude_desktop_config.json"
    else:
        exe = str(venv_bin / "delegation-core")
        desktop_config_path = "~/.config/Claude/claude_desktop_config.json"

    mcp_snippet = (
        '{\n'
        '  "mcpServers": {\n'
        '    "delegation-core": {\n'
        f'      "command": "{exe}",\n'
        '      "args": ["run"]\n'
        '    }\n'
        '  }\n'
        '}'
    )

    agent_guide = CONFIG_DIR / "AGENT_GUIDE.md"
    system_prompt = CONFIG_DIR / "CLAUDE_SYSTEM_PROMPT.md"
    # The installer writes this into ~/.claude/settings.json itself
    # (clients.register_session_hooks); shown here so it can be checked.
    from .clients import hook_executable
    exe = hook_executable()
    hooks_snippet = (
        '{\n'
        '  "hooks": {\n'
        '    "SessionStart": [\n'
        '      { "matcher": "*", "hooks": [\n'
        f'        {{ "type": "command", "command": "\\"{exe}\\" session-start" }}\n'
        '      ]}\n'
        '    ],\n'
        '    "SessionEnd": [\n'
        '      { "matcher": "*", "hooks": [\n'
        f'        {{ "type": "command", "command": "\\"{exe}\\" session-end" }}\n'
        '      ]}\n'
        '    ]\n'
        '  }\n'
        '}'
    )

    claude_md_snippet = (
        "# delegation-core\n\n"
        "Follow this protocol whenever delegation-core's MCP tools are available:\n\n"
        f"@{agent_guide}"
    )

    console.print(Panel.fit(
        "[bold green]Setup complete![/bold green]\n\n"
        "A few more steps wire delegation-core into Claude Code and Claude Desktop\n"
        "so both surfaces share this vault as memory. Each step below is optional:\n"
        "skip any you don't need, or come back to this later.",
        border_style="green",
    ))

    console.print()
    console.print("  [bold]1. Claude Desktop: register the MCP server[/bold]")
    console.print("     Add this block to your Claude Desktop config, then restart Claude Desktop.")
    console.print()
    console.print(Panel(mcp_snippet, title=f"[bold]{desktop_config_path}[/bold]", border_style="blue"))

    console.print()
    console.print("  [bold]2. Claude Code: register the same MCP server[/bold]")
    console.print("     Merge this into the [cyan]mcpServers[/cyan] key of [cyan]~/.claude.json[/cyan].")
    console.print()
    console.print(Panel(mcp_snippet, title="[bold]~/.claude.json[/bold]", border_style="blue"))

    console.print()
    console.print("  [bold]3. Claude Code: session hooks[/bold]")
    console.print("     The installer registers these in [cyan]~/.claude/settings.json[/cyan]. The SessionStart hook")
    console.print("     briefs Code on vault activity and runs maintenance on a non-empty inbox;")
    console.print("     the SessionEnd hook backs up the raw transcript to the vault.")
    console.print()
    console.print(Panel(hooks_snippet, title="[bold]~/.claude/settings.json[/bold]", border_style="blue"))

    console.print()
    console.print("  [bold]4. Claude Code: load the agent protocol every session[/bold]")
    console.print("     Add this to [cyan]~/.claude/CLAUDE.md[/cyan] (create it if missing).")
    console.print()
    console.print(Panel(claude_md_snippet, title="[bold]~/.claude/CLAUDE.md[/bold]", border_style="blue"))

    console.print()
    console.print("  [bold]5. Claude Desktop / Cowork: load the agent protocol[/bold]")
    console.print("     There's no config file for this: open Claude Desktop's")
    console.print("     [cyan]Settings → Custom Instructions[/cyan] (and/or each Cowork project's")
    console.print("     instructions) and paste the contents of:")
    console.print(f"       [bold]{system_prompt}[/bold]")

    console.print()
    console.print("  Verify everything is working:")
    console.print("    [bold]delegation-core status[/bold]\n")


# ── helpers ───────────────────────────────────────────────────────────────────

def _welcome():
    console.print()
    console.print(Panel.fit(
        "[bold cyan]delegation-core[/bold cyan]  setup\n"
        "[dim]Local AI for Claude · takes about 5 minutes[/dim]",
        border_style="cyan",
        padding=(1, 4),
    ))
    console.print()


def _header(step: str, title: str):
    console.print()
    console.print(Rule(f"[bold]{step} : {title}[/bold]", style="cyan"))
    console.print()


def _menu(title: str, options: list) -> int:
    console.print(f"  {title}\n")
    for i, opt in enumerate(options, 1):
        console.print(f"    [bold cyan]{i}[/bold cyan]  {opt}")
    console.print()
    while True:
        raw = console.input(f"  Enter number [1-{len(options)}]: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        console.print(f"  [red]Please enter a number between 1 and {len(options)}.[/red]")


def _menu_index(prompt: str, count: int) -> int:
    while True:
        raw = console.input(f"  {prompt} [1-{count}]: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= count:
            return int(raw) - 1
        console.print(f"  [red]Please enter a number between 1 and {count}.[/red]")


