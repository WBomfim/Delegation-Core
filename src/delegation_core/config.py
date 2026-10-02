"""
config.py: User configuration stored at ~/.delegation_core/config.json.

v0.2 additions:
  synthesis_enabled  toggle the LLM synthesis pipeline (default True)
  synthesis_lang     language for synthesis prompts: "en" | "pt" (default "en")
  budget_mode        "normal" | "cpu" | "auto": auto measures tok/sec at startup
                     and computes per-task budgets that stay within mcp_timeout_sec
  ingest_chunk_size  chunk size for external ingestion
  ingest_chunk_overlap  overlap for external ingestion chunks

v0.3 additions:
  split_min_chars    files larger than this are split into multiple notes
  split_max_notes    maximum notes produced per recursive split (default 10)

v0.4 additions:
  tok_sec              measured tokens/sec (0 = not calibrated yet)
  mcp_timeout_sec      budget ceiling for auto mode (default 60s)
  quality_threshold    synthesis scores below this trigger repair (default 0.50)
  heal_per_run         max notes re-synthesized per maintenance pass (default 10)
  never_merge_folders  folders excluded from merge and heal passes (default: sessions)

v0.5.1 additions:
  web_search_enabled  opt-in DuckDuckGo web search (default False). Off by
                      default because it reaches the public internet, outside
                      the local-only design. Requires the [web] extra:
                      pip install "delegation-core[web]"
  engine_mode         where generation runs: "local" (llama.cpp, default) or
                      "agent" (no local model: synthesis/compression delegated
                      to the calling Claude). Embeddings + search stay local in
                      both modes. "agent" is for machines that can't spare the
                      RAM/CPU to run a local model alongside other apps.

v0.7.0 additions:
  graphs_dir, graphs_registry_path  storage for the vendored code-graph pipeline
                      (graph_build/graph_list/graph_report tools). Opt-in via the
                      [graph] extra: see delegation_core/graph/__init__.py.
"""

import json
import logging
import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger("config")

CONFIG_DIR = Path.home() / ".delegation_core"
CONFIG_FILE = CONFIG_DIR / "config.json"

def vault_health_cache() -> Path:
    """O caminho do cache de saude do vault. UMA definicao, lida em tempo de chamada.

    Era montado com `Path.home() / ".delegation_core" / "vault_health.json"` em
    SEIS lugares, dentro das funcoes, e com CONFIG_DIR num setimo (installer).
    Um caminho montado a partir de Path.home() nao tem nome para ninguem
    reatribuir: o fixture do conftest, que promete "reaponta todo caminho de
    estado do usuario", alcanca constante de modulo e nao alcanca aquilo.

    O que custava, medido em 04/09/2026: rodar
    `pytest tests/test_frontmatter_valido.py` sobre um vault temporario de DUAS
    notas reescreveu o vault_health.json REAL do usuario, com
    vault_path=/tmp/pytest-of-joey/... e total_notes=2. E tres dos seis lugares
    fazem unlink(), entao um teste que escrevesse nota APAGAVA o cache real.

    Funcao e nao constante de proposito: uma constante seria copiada por
    `from .config import ...` no import de cada modulo, que e exatamente a
    armadilha que o conftest ja documenta para CONFIG_DIR. Lida daqui, um
    redirecionamento de CONFIG_DIR chega a todos os chamadores.

    Servir numero de outro vault ja era impedido: o cache carrega `vault_path` e
    `get_health_summary` recusa um cache alheio. Essa guarda nasceu de "two
    tests over different temp vaults returned identical health" -- trataram o
    sintoma desta mesma causa e deixaram o caminho fixo.
    """
    return CONFIG_DIR / "vault_health.json"


def resolve_folder(name: str, folders: list) -> str | None:
    """Return the canonically-cased entry of `folders` matching `name`, else None.

    Vault folder names are user-defined: the shipped defaults are lowercase
    ("decisions", "research", ...), but a vault configured through the wizard or
    edited by hand commonly uses Capitalized names ("Decisions", "Reference").
    Code that hardcoded a lowercase name and tested `"sessions" in folders`
    silently did the wrong thing on such a vault: export_session wrote to
    folders[0], the classifier's folder hints and its own returned label never
    matched, and orphan accounting skipped nothing. Matching is case-insensitive
    but the *caller* always gets back the real folder name, so the value is safe
    to use directly as a path segment.
    """
    if not name:
        return None
    target = name.strip().lower()
    for f in folders:
        if str(f).strip().lower() == target:
            return f
    return None


@dataclass
class Config:
    # ── vault ────────────────────────────────────────────────────────────────
    vault_path: str = ""
    vault_folders: list = field(default_factory=lambda: [
        "decisions", "research", "tools", "fixes", "reference", "sessions"
    ])

    # ── llama.cpp ────────────────────────────────────────────────────────────
    llama_binary: str = ""
    llama_model: str = ""
    llama_port: int = 8181
    llama_ctx: int = 4096
    llama_ngl: int = 999   # GPU layers to offload (999 = all)
    #: Let a reasoning model spend the token budget on its private thought
    #: channel. Off, because leaving it on fails silently: the model writes
    #: into `reasoning_content` first and only then into `content`, so a
    #: budget that runs out mid-thought returns an EMPTY answer with HTTP 200
    #: and no error anywhere. Measured on this machine with gemma-4-12B, the
    #: prompt "Responda apenas: TESTE OK" cost 1045 completion tokens with
    #: thinking on and 4 with it off, same answer. Ten queued reviews of real
    #: source files came back empty before this existed.
    llama_enable_thinking: bool = False

    # ── embeddings ───────────────────────────────────────────────────────────
    bge_model: str = "BAAI/bge-base-en-v1.5"

    # ── similarity thresholds ────────────────────────────────────────────────
    search_threshold: float = 0.55
    merge_threshold: float = 0.88

    # ── inference defaults ───────────────────────────────────────────────────
    max_tokens: int = 2048

    # ── v0.2: synthesis pipeline ─────────────────────────────────────────────
    synthesis_enabled: bool = True
    synthesis_lang: str = "en"   # "en" | "pt"

    # ── v0.2: hardware budget mode ───────────────────────────────────────────
    # "cpu": applies strict token caps to stay within the 120s MCP timeout on
    #        CPU-only machines (SAAD deployment pattern).
    # "normal": no additional caps beyond max_tokens.
    budget_mode: str = "normal"

    # ── v0.2: external ingestion (ABNER) ─────────────────────────────────────
    ingest_chunk_size: int = 3072
    ingest_chunk_overlap: int = 200

    # ── v0.12: vault note chunking ───────────────────────────────────────────
    # Until v0.12 a vault note was indexed as ONE ChromaDB row holding the whole
    # file. Every embedding model has a hard input ceiling, so anything past it
    # was silently dropped: measured on a real vault, a 122k-token code-graph
    # report was 6.7% represented in the index and a 60k-token transcript 13.6%.
    # The remainder was unsearchable with nothing anywhere reporting it missing.
    # Notes are now chunked the way ingest.py has always chunked external files.
    # Sized in CHARACTERS (chunk_text splits on characters), see
    # embed_max_seq_length for the token ceiling these must stay under (3072 chars
    # cleanly aligns with 1024 tokens for bge-m3 / bge-base).
    vault_chunk_size: int = 3072
    vault_chunk_overlap: int = 200

    # ── v0.12: embedding execution limits ────────────────────────────────────
    # Both pinned explicitly rather than inherited from the model card. bge-m3
    # advertises 8192, and transformer attention costs batch x seq^2: a batch of
    # 8 at 8192 asks roughly 32 GB of attention buffer, which OOMs a 16 GB card
    # mid-reindex (observed in production, not theorised). With chunking above,
    # no chunk comes near this ceiling, so lowering it costs no recall.
    # 0 means "leave the model's own default alone".
    embed_max_seq_length: int = 2048
    embed_batch_size: int = 8
    #: "auto" (o acelerador que existir), "cpu", "cuda" ou "mps". Existe porque
    #: o BGE-m3 e o llama-server nao cabem juntos numa placa de 16 GB, e ate
    #: 09/09/2026 a unica forma de por o encoder na CPU era esconder a placa do
    #: processo inteiro com CUDA_VISIBLE_DEVICES= num drop-in de systemd.
    embed_device: str = "auto"

    # ── v0.12: default search scope ──────────────────────────────────────────
    # "" means adaptive (decided per vault from how much of it is generated).
    # A fixed "notes" default was right for the vault it was measured on (3,692
    # generated articles against 187 hand-written notes) and exactly wrong for a
    # vault whose authoritative material is ingested: one field deployment had
    # 6,637 external files invisible to the default every agent uses, and 3 of 4
    # probe queries returned a raw transcript where scope='all' returned the
    # authoritative document. Set explicitly to pin one scope for this machine.
    default_search_scope: str = ""

    # ── v0.13: client scoping ────────────────────────────────────────────────
    # A vault organised by client had no way to say "only this one". Searching
    # for one client's retention metrics returned six of ten results from
    # another client, and there was no parameter that could exclude them.
    # `client:` in a note's frontmatter is promoted to searchable metadata,
    # normalised through client_slug so Gazin and gazin land in one bucket.
    #
    # client_path_roots makes the same filter reach INGESTED files, which are
    # the bulk of a real index (92.8% of rows on the deployment that reported
    # this). The client is the path segment directly under a listed root:
    # "/Work/Oksigen" turns /Work/Oksigen/Gazin/deck.pdf into client "gazin".
    # Empty by default: deriving a client from an unconfigured path shape would
    # invent labels, and a wrong label silently excludes a document from the
    # filter that should have found it, which is worse than no label at all.
    client_path_roots: list = field(default_factory=list)
    # {slug: canonical slug}, for names that normalisation cannot merge because
    # they are genuinely different strings ("campo-incorporadora" -> "campo").
    # That is a judgement about the data, so it is configuration, not code.
    client_aliases: dict = field(default_factory=dict)

    # ── v0.12: subkind ranking weights (raw vs curated) ──────────────────────
    # Dampens raw verbose transcripts/chat dumps so authoritative curated notes
    # rank higher when semantic similarity is close. Default 1.0 for curated.
    subkind_weights: dict = field(default_factory=lambda: {
        "curated": 1.0,
        "chat": 0.98,
        "transcript": 0.95,
        "generated": 1.0,
        "external": 1.0,
    })

    # ── v0.3: recursive note splitting ───────────────────────────────────────
    # Files larger than split_min_chars trigger the three-tier split strategy.
    # PDFs with > 1 extractable page are always split regardless of char count.
    split_min_chars: int = 3000
    split_max_notes: int = 10

    # ── v0.4: quality + healing ───────────────────────────────────────────────
    tok_sec: float = 0.0
    mcp_timeout_sec: int = 60
    quality_threshold: float = 0.50
    heal_per_run: int = 10
    never_merge_folders: list = field(default_factory=lambda: ["sessions"])

    # ── daemon liveness ──────────────────────────────────────────────────────
    # Seconds the daemon's event loop may stay stuck before the process dumps
    # every thread's stack and exits 1, so launchd/systemd start a fresh one.
    # 0 turns it off. A stuck loop answers nobody: clients give up after 10-30s,
    # so 300 only ever ends a process that was already dead to every caller.
    loop_watchdog_sec: int = 300

    # ── onde mora o indice ───────────────────────────────────────────────────
    # Vazio: `<vault>/.chroma_bge`, como sempre. Preenchido: o indice mora ali.
    # Existe para o vault que fica numa pasta sincronizada (OneDrive, iCloud):
    # sincronizacao mexendo no SQLite do Chroma sob um processo aberto danifica
    # o indice. A recuperacao automatica preenche este campo sozinha quando poe
    # em quarentena um indice nessa situacao. Ver recuperacao.py.
    index_path: str = ""

    # ── v0.13.1: guard against a second index writer ─────────────────────────
    # When no daemon answers, index commands do the work in this process. That
    # is what keeps the CLI usable on a machine that never installed the
    # service, and it is also how a hook-fired `reindex` becomes a second
    # writer against an index the running daemon holds open, which is the
    # sequence that corrupted an index in the field: under load the daemon
    # stops answering without dying, "no daemon" reads as true, and two
    # processes write the same ChromaDB.
    #
    # Turn this off on a machine where the daemon is the only writer. The
    # fallback then refuses instead of writing. `--local` still wins, because
    # that is an operator asking on purpose rather than a hook guessing.
    allow_local_index_fallback: bool = True

    # ── v0.5.1: optional web search ──────────────────────────────────────────
    # Opt-in. Reaches the public internet via DuckDuckGo, so it is off by
    # default and its dependency (duckduckgo-search) ships as the [web] extra.
    web_search_enabled: bool = False

    # ── v0.5.1: engine mode ──────────────────────────────────────────────────
    # "local"  : run the model locally via llama.cpp (default).
    # "agent"  : no local model. Synthesis/compression is delegated to the
    #            calling MCP client (Claude): interactive tools return the raw
    #            retrieved material for the agent to reason over, and background
    #            maintenance uses a deterministic extractive fallback.
    # "hybrid" : best of both. Interactive/light work is delegated to Claude
    #            (fast, no local load). Big/slow/bulk generation uses the LOCAL
    #            model instead: background pipelines (synthesize/heal/ingestion)
    #            route local automatically (no agent is in the loop there), and
    #            oversized interactive inputs are NOT auto-run: the tool returns
    #            a token-cost estimate plus an explicit offer to run locally
    #            (pass use_local=true) so the choice is surfaced, never silent.
    # BGE embeddings + ChromaDB search always run locally in every mode.
    engine_mode: str = "local"

    # ── v0.5.1: hybrid routing ───────────────────────────────────────────────
    # In hybrid mode, an interactive input at or above this many characters is
    # treated as "big": instead of delegating to Claude, the tool returns a
    # cost estimate and offers the local model (opt-in via use_local=true).
    hybrid_local_min_chars: int = 8000

    # ── v0.11: HTTP transport ────────────────────────────────────────────────
    # The server is a single long-lived daemon that every MCP client connects
    # to over HTTP, replacing the previous stdio model where each client
    # spawned its own process (and its own BGE copy, and its own ChromaDB
    # writer, see client_tracking.py's module docstring for what that cost).
    #
    # server_host stays on loopback. Nothing here is designed to be reachable
    # from another machine: the token below is a guard against other *local*
    # processes, not a substitute for network isolation.
    server_host: str = "127.0.0.1"
    server_port: int = 8797
    server_path: str = "/mcp"

    # Bearer token every client must present. Generated on first use by
    # ensure_server_token(); empty means "not yet generated", never "no auth".
    # dashboard_api.py learned this the hard way in v0.8.1: an unauthenticated
    # local port let any website in the user's browser read and write the vault
    # via a guessed port. The surface here is the whole vault plus every tool,
    # so there is no unauthenticated mode to fall back to.
    server_token: str = ""

    # The dashboard's JSON API, served by the daemon on a second loopback port.
    # It used to be a sidecar the Tauri app spawned, which was the right shape
    # under stdio (mcp.run() served one transport), so the dashboard could not
    # attach to the MCP server and needed its own process. That process then
    # built its own VaultManager: measured on this machine, opening the
    # dashboard added a second 2314 MiB copy of BGE-m3 to the GPU and a second
    # ChromaDB opener on the same index, which is exactly what moving to a
    # daemon removed for every other client.
    #
    # The daemon already holds a warm VaultManager, so it serves those routes
    # itself. 0 disables it, and the sidecar entry point still works standalone
    # for a machine running no daemon.
    dashboard_port: int = 8788

    # How long the local model may sit loaded with an empty task line before the
    # worker unloads it. A queued task pins ~11.5 GiB on this machine (gemma-12B
    # Q6 alongside BGE-m3, measured: 3838 -> 15386 MiB), and without this a
    # single task holds that until the next daemon restart, reintroducing
    # exactly the GPU contention that engine_mode "agent" exists to avoid.
    #
    # Only applies in agent mode, where the local model runs solely to serve the
    # queue. In local/hybrid mode it is the engine every other caller uses, and
    # unloading it under them would turn one idle minute into a 10s reload on
    # the next call. 0 disables the unload entirely.
    local_idle_shutdown_sec: int = 300

    # ── v0.11: local-model queue ─────────────────────────────────────────────
    # How many llama.cpp requests may be in flight at once. One daemon now
    # fronts every client, so without this a burst of clients would stampede a
    # single local model. 1 keeps the model's own batching predictable; raise
    # it only if the model is served with real parallel slots.
    llama_queue_concurrency: int = 1

    # Ingestion source policy stays last because these lists can be long.
    # Sources are opt-in. An empty list preserves one-off `ingest_folder(path)`;
    # once populated, ingestion accepts only enabled entries declared here.
    ingest_sources: list[dict] = field(default_factory=list)
    # A pattern can match a directory (`node_modules`), a filename (`*.log`),
    # or a source-relative path (`exports/private/*`).
    ingest_exclude_patterns: list[str] = field(default_factory=lambda: [
        ".git", "node_modules", "__pycache__", ".cache", ".mypy_cache",
        ".pytest_cache", ".ruff_cache", ".tox", ".venv", "venv", "dist",
        "build", "coverage", "htmlcov",
    ])

    # ── derived ──────────────────────────────────────────────────────────────

    @property
    def llama_url(self) -> str:
        return f"http://localhost:{self.llama_port}"

    @property
    def server_url(self) -> str:
        """The MCP endpoint clients put in their config. Always loopback."""
        return f"http://{self.server_host}:{self.server_port}{self.server_path}"

    @property
    def vault(self) -> Path:
        return Path(self.vault_path).expanduser()

    @property
    def chroma_path(self) -> Path:
        if str(self.index_path or "").strip():
            return Path(self.index_path).expanduser()
        return self.vault / ".chroma_bge"

    @property
    def collection_name(self) -> str:
        """ChromaDB collection for the configured embedding model.

        Derived rather than fixed so two models can be indexed side by side in the
        same store: their vector dimensions differ (768 vs 1024), so a shared
        collection would reject the second one outright. With one collection each,
        switching models is a config edit: the other index is still there.
        """
        from .embeddings import collection_name_for
        return collection_name_for(self.bge_model)

    @property
    def log_path(self) -> Path:
        return CONFIG_DIR / "server.log"

    @property
    def llama_log_path(self) -> Path:
        return CONFIG_DIR / "llama_cpp.log"

    @property
    def models_dir(self) -> Path:
        return CONFIG_DIR / "models"

    @property
    def llama_dir(self) -> Path:
        return CONFIG_DIR / "llama"

    @property
    def processes_path(self) -> Path:
        return CONFIG_DIR / "processes.json"

    @property
    def graphs_dir(self) -> Path:
        return CONFIG_DIR / "graphs"

    @property
    def graphs_registry_path(self) -> Path:
        return CONFIG_DIR / "graphs_registry.json"

    @property
    def is_cpu_budget(self) -> bool:
        return self.budget_mode == "cpu"

    @property
    def is_agent_mode(self) -> bool:
        """True when generation is delegated to the calling Claude (no local model)."""
        return self.engine_mode == "agent"

    @property
    def is_hybrid_mode(self) -> bool:
        """True when light work goes to Claude but big/bulk work uses the local model."""
        return self.engine_mode == "hybrid"

    @property
    def uses_local_model(self) -> bool:
        """True when this mode may need llama.cpp (local always; hybrid for big/bulk)."""
        return self.engine_mode in ("local", "hybrid")

    # Background/bulk generation tasks: no agent is in the loop when these run
    # (fire-and-forget maintenance/ingestion), so in hybrid they route to the
    # local model rather than being delegated to Claude.
    _HEAVY_TASKS = frozenset({"synthesize", "heal", "review_body"})

    def route(self, task: str = "default", input_chars: int = 0, use_local: bool = False) -> str:
        """Decide where a generation call runs. Returns:
          "local" : run llama.cpp locally
          "agent" : delegate to the calling Claude (return raw material)
          "offer" : big interactive input: don't auto-run; return a cost estimate
                    and offer the local model (caller re-invokes with use_local)
        Considers engine_mode + task type + input size + explicit opt-in.
        """
        if self.engine_mode == "local":
            return "local"
        if self.engine_mode == "agent":
            return "agent"
        # hybrid: nuanced routing
        if use_local:
            return "local"
        if task in self._HEAVY_TASKS:
            return "local"                       # background/bulk always local
        if input_chars >= self.hybrid_local_min_chars:
            return "offer"                       # big: surface the choice + cost
        return "agent"                           # light interactive -> Claude

    def local_index_fallback_allowed(self) -> bool:
        """Whether a command may write the index in this process.

        Two ways to say no, because they answer different needs: the config
        flag is the durable setting, and a `no_auto_reindex` file in the state
        directory is the switch you can flip without editing JSON, and, unlike
        a patched hook, neither is erased by the next install.
        """
        if not self.allow_local_index_fallback:
            return False
        return not (CONFIG_DIR / "no_auto_reindex").exists()

    def is_configured(self) -> bool:
        if self.is_agent_mode:
            return bool(self.vault_path)
        return bool(self.vault_path and self.llama_binary and self.llama_model)

    # ── persistence ──────────────────────────────────────────────────────────

    @classmethod
    def load(cls) -> "Config":
        if CONFIG_FILE.exists():
            try:
                try:
                    CONFIG_FILE.chmod(0o600)
                except OSError:
                    pass
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
                cfg = cls(**known)
                if "search_threshold" not in data:
                    from .embeddings import profile_for
                    cfg.search_threshold = profile_for(cfg.bge_model).get("search_threshold", 0.50)
                return cfg
            except Exception as e:
                logger.error("Could not load %s: %s (using defaults)", CONFIG_FILE, e)
        return cls()

    def save(self):
        """Persist the config, atomically, never leaving a half-written one.

        `write_text` truncates the destination before writing, so a process that
        dies mid-write leaves a config that `load()` cannot parse — and `load()`
        answers an unparseable file with `cls()`, meaning DEFAULTS, meaning no
        vault_path, meaning "not configured". That is the exact state that took
        this machine out of service on 2026-09-02 (by a different cause: a test
        that called calibrate() and wrote a real config with a tmp vault_path).
        The conftest guard in the test suite exists because of that day.

        Six other state files in this package already write tmp+fsync+replace,
        each with a comment explaining this failure mode — localqueue, jobs,
        tracker, windows, ingest, clients. This one, the file the daemon cannot
        start without, was the one left out.

        The 0600 goes on the temporary file BEFORE the replace, not on the
        destination after it. server_token lives in here, and writing first and
        chmod-ing second leaves a window where the secret sits at the umask
        default. chmod is a no-op for this purpose on Windows, where the user
        profile directory is what actually restricts access.
        """
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        dados = json.dumps(asdict(self), indent=2)
        tmp = CONFIG_FILE.with_suffix(".json.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                try:
                    os.chmod(tmp, 0o600)
                except (OSError, AttributeError):
                    pass
                fh.write(dados)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, CONFIG_FILE)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def ensure_server_token(self) -> str:
        """Return the bearer token, generating and persisting one on first call.

        Called from the server's startup path rather than only from `setup`, so
        an install that predates HTTP transport (or a config edited by hand)
        still comes up authenticated instead of coming up open.
        """
        if not self.server_token:
            self.server_token = secrets.token_urlsafe(32)
            self.save()
        return self.server_token


# ── a frase de idioma, num lugar so ─────────────────────────────────────────

#: O que `synthesis_lang` diz a cada prompt que produz PROSA para uma pessoa ler.
#:
#: O ajuste existe desde a v0.2 e ate 03/09/2026 um unico modulo o lia,
#: `synthesizer.py`. Todo o resto montava prompt em ingles e deixava o modelo
#: local escolher o idioma da resposta, o que visto de fora faz o ajuste parecer
#: simplesmente quebrado: um vault inteiro em portugues, com
#: `synthesis_lang: "pt"` na config, recebia o resumo do `search_vault` em
#: ingles.
#:
#: Mora aqui, e nao em cada ponto de chamada, porque a copia ja tinha divergido
#: uma vez: o `compress` da ferramenta MCP ganhou a instrucao e o `cmd_compress`
#: do CLI, que e a mesma operacao no terminal, nao. Dentro do mesmo commit.
INSTRUCOES_DE_IDIOMA = {
    "pt": "Responda em portugues do Brasil.",
    "en": "Answer in English.",
}


def lang_instruction(cfg) -> str:
    """A frase de idioma para esta configuracao, ou vazio se o valor e estranho.

    Vazio e resposta legitima: um `synthesis_lang` que ninguem reconhece nao deve
    virar instrucao inventada, deve virar prompt identico ao que era antes disto
    existir.
    """
    valor = (getattr(cfg, "synthesis_lang", "en") or "en").lower()
    return INSTRUCOES_DE_IDIOMA.get(valor, "")


def with_lang(system: str, cfg) -> str:
    """Anexa a frase de idioma a um `system`, sem sujeira quando ela e vazia.

    A juncao ingenua (`f"{system} {frase}"`) deixa um espaco pendurado no fim
    quando a frase nao existe, e esse e exatamente o tipo de detalhe que so
    aparece na configuracao que ninguem testa.
    """
    frase = lang_instruction(cfg)
    if not frase:
        return system
    return f"{system.rstrip()} {frase}"
