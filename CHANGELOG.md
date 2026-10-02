# Changelog

All notable changes to the Delegation-Core Office project (v0.1.0 to v0.13.0 / v13) are documented in this file.
This changelog is derived directly from the canonical versioning recorded across the codebase and vault archives.

---

## Unreleased

### Added

- **Declarative ingestion configuration**: Added `ingest_sources` and exclusion
  patterns to configure allow-listed external document sources, per-source
  settings, and safe exclusions before ingestion.
- **Ingestion configuration guide**: Documented when and how to configure
  recurring external sources after the initial setup.

### Fixed

- **Windows automatic startup**: Recreated the Startup launcher through the
  virtual environment Python runtime instead of the blocked legacy executable.
- **Cross-platform service checks**: Made service definition tests select the
  platform-specific launcher behavior.
- **Vault index reporting**: Distinguished physical Markdown files, indexed
  documents, and ChromaDB chunks in startup metrics.
- **Configuration permission handling**: Saved configuration permissions using
  a filesystem path compatible with Windows.

### Changed

- **Repository hygiene**: Organized `.gitignore` rules into documented blocks.

---

## Unreleased (2026-10-01) - Default MCP port

### Changed
- The default MCP port is now `8797` instead of `8787`, avoiding a collision with Headroom's commonly used proxy port. Explicit `server_port` values in existing configurations are preserved. Client configuration examples use the new default.

## Unreleased (2026-09-29) - Daemon travado no Mac: reabertura do indice, loop bloqueado e watchdog

### Fixed
- **Reabertura do indice vazava o motor do chromadb.** `clear_system_cache()` so esquecia o System antigo, sem parar; medido no chromadb 1.5.9, cada reabertura deixava 27 threads rodando (234 de 285 no daemon do Mac em 26/09). A reabertura agora chama `Client.close()` (`index_lock.close_chroma_client`).
- **Reabertura sob consulta em andamento.** Toda chamada que usa `collection` segura uma trava compartilhada; a reabertura espera com a trava exclusiva. Pedida de dentro de uma consulta, ela e adiada para a chamada seguinte, em vez de travar em si mesma.
- **Ferramentas bloqueantes no event loop.** 17 ferramentas sem `await` viraram `def`, que o fastmcp roda no threadpool; `search_vault` e `heartbeat` levam o trabalho do indice para `asyncio.to_thread`. As que escrevem continuam serializadas entre si.
- **`status` e `embed-model` abriam o indice ao lado do daemon.** Abrir um PersistentClient, mesmo so para contar, muda o mtime do `chroma.sqlite3`, e o daemon le isso como escrita de outro processo e reabre. Com o daemon no ar eles perguntam a ele; um daemon que aceita conexao e nao responde e reportado em vez de contornado.

### Added
- **Watchdog do event loop** (`loop_watchdog_sec`, padrao 300, 0 desliga). Um timer do faulthandler, que roda sem o GIL, e rearmado pelo loop; se o loop parar, grava a pilha de todas as threads em `~/.delegation_core/watchdog_tracebacks.log` e sai com codigo 1, que launchd e systemd reiniciam.

### Not in this change
- O indice ja danificado no Mac (SIGSEGV em `chromadb_rust_bindings` desde 29/09 07:43) precisa de recuperacao propria; esta mudanca previne, nao repara.
- Troca do modelo padrao para bge-m3 e a lista de pastas do AGENT_GUIDE no Mac: ajustes de configuracao por maquina.

---

## v0.13.0 / v13 (2026-09-04) - Delegation-Core Office: Consolidacao de 49 Defeitos de Campo, Ponte Claude Desktop, Instaladores Python e Blindagem de AST

### Added & Rebranded
- **Project Rebranding to Delegation-Core Office**: Official renaming of the repository and project identity to Delegation-Core Office.
- **Native Claude Desktop stdio bridge (`src/delegation_core/stdio_bridge.py`)**:
  - Implemented an in-process FastMCP stdio proxy (`mcp-stdio`) that forwards JSON-RPC communication directly to the local HTTP daemon.
  - Solves the fundamental Claude Desktop limitation where remote `type: http` entries with `url` caused Claude Desktop to overwrite `claude_desktop_config.json` and delete the entire `mcpServers` block.
  - Eliminates external dependencies on Node.js and `mcp-remote`, avoiding multi-daemon port and VRAM collisions.
- **Unified Python-based installation and update engine (`src/delegation_core/installer.py`)**:
  - Migrated 810 lines of disparate shell (`install.sh`, `uninstall.sh`) and batch (`install.bat`, `uninstall.bat`) scripts into modular, fully testable Python.
  - Shell and Batch files shrunk to thin runtime-detection stubs.
  - New CLI subcommand `delegation-core update`: performs automated package upgrades, service restarts, client config repairs, and vault health checks in a single atomic command.
  - Added robust service control commands (`stop`, `start`, `restart`, `is_up`) across systemd (Linux), launchd (macOS), and Task Scheduler (Windows).
- **GPU Arbiter for VRAM Mutual Exclusion (`src/delegation_core/gpu.py`)**:
  - Implemented `GpuArbiter` with `threading.RLock()` to manage single-GPU contention between the 12B local LLM (`llama-server`) and `BAAI/bge-m3` embedding model.
  - Coordinated three-tier memory eviction (SentenceTransformer class cache, ChromaDB collection pointers, and PyTorch CUDA cache reclamation) to eliminate `cudaMalloc` out-of-memory errors on 16 GB GPUs.
- **Stamped Incremental Indexing (`stamp_indexed()` in `src/delegation_core/vault.py`)**:
  - Stamped timestamps on successfully indexed notes to eliminate redundant vector re-embeddings.
  - Reduced incremental reindex duration on 4,800+ note vaults from 11 minutes to under 2 seconds.
- **Modular Note Management (`src/delegation_core/notes.py`)**:
  - Extracted note authoring, frontmatter sanitization, and title alias management out of `vault.py` into a dedicated domain module.
- **Systemic Test Guarding (`tests/conftest.py`)**:
  - Added global isolation fixtures to guarantee that automated test runs never mutate or overwrite the user's real Obsidian vault or configuration.
  - Test suite expanded to 1,747 automated tests (100% green).

### Fixed & Hardened (49 Defeitos de Campo Corrigidos)

#### Grupo 1: CLI, Daemon e Concorrencia de GPU
- **1. `note write` e `note update` escreviam no indice por fora do daemon (`cli.py`)**:
  - *Defeito:* A CLI criava um VaultManager local para indexar a nota em vez de delegar ao daemon ativo, instanciando uma segunda copia de 2.3 GB do BGE-m3 na GPU.
  - *Correcao:* Chamadas de escrita e atualizacao foram roteadas para a API do daemon HTTP via loopback.
- **2. `wikilinks()` sugeria links para notas ja apagadas (`linker.py`)**:
  - *Defeito:* O resolvedor consultava apenas o indice em memoria sem checar se o arquivo correspondente ainda existia no disco.
  - *Correcao:* Adicionada validacao de existencia fisica antes de injetar wikilinks de retorno.
- **3. `typical_seconds` calculava mediana sobre distribuicao bimodal (`jobs.py`)**:
  - *Defeito:* O calculo de tempo tipico misturava jobs curtos (reindex incremental de 70ms) com jobs longos (reindex forcado de 3 minutos), gerando estimativas erradas de pacing.
  - *Correcao:* As metricas foram segregadas por tipo de carga e parametros de execucao.
- **4. `note write` destruia nota anterior do mesmo dia (`notes.py`)**:
  - *Defeito:* Criar uma segunda nota no mesmo dia com titulo semelhante sobrescrevia o arquivo anterior em vez de gerar um sufixo desambiguador.
  - *Correcao:* Implementada geracao de sufixo numerico atomico na colisao de nomes.
- **21. `relink` e `graph build` escreviam no indice por fora do daemon (`cli.py`)**:
  - *Defeito:* Comandos de manutencao pesada criavam instancias paralelas do BGE na VRAM.
  - *Correcao:* Delegado todo o processo de reindexacao e escrita para a fila do daemon.
- **22. Leituras da CLI (`search`) carregavam segundo modelo BGE na GPU (`cli.py`)**:
  - *Defeito:* Buscas pontuais disparavam o carregamento de embeddings localmente enquanto o daemon ja mantinha o modelo residente na memoria de video.
  - *Correcao:* Roteamento da busca CLI para o endpoint `/mcp` do daemon.
- **23. Falso positivo no diagnostico `doctor.py` (`doctor.py`)**:
  - *Defeito:* O check `local_fallback` reportava "the daemon is the only writer" sem inspecionar os processos reais no sistema operacional.
  - *Correcao:* Verificacao ativa de descritores de arquivo e locks abertos antes de atestar exclusividade.
- **25. `stdio_bridge` nao validava preenchimento de credenciais (`stdio_bridge.py`)**:
  - *Defeito:* A ponte verificava se o daemon estava escutando na porta, mas nao validava se o token Bearer estava configurado, enviando `Authorization: Bearer ` vazio e gerando erro 401 silencioso.
  - *Correcao:* Validacao previa da existencia e formato do token na subida da ponte.
- **28. Endpoints do Dashboard desprotegidos (`dashboard_api.py`)**:
  - *Defeito:* A porta 8788 servia acoes de escrita e leitura de tarefas sem verificacao de token ou isolamento rigoroso.
  - *Correcao:* Blindagem dos endpoints e amarracao estrita ao loopback.

#### Grupo 2: Setup, Instaladores e Servicos Multiplataforma
- **5. Caractere `%` no caminho quebrava o systemd (`wizard.py`)**:
  - *Defeito:* O systemd interpreta `%h` e `%n` como especificadores de formatacao. Um diretorio com `%` (ex: `100%hits`) impedia a inicializacao do servico no boot.
  - *Correcao:* Escapamento de `%` para `%%` na geracao da unit do systemd.
- **6. Caractere `&` no caminho quebrava o launchd no macOS (`wizard.py`)**:
  - *Defeito:* Arquivos `.plist` em XML geravam erro de sintaxe ("invalid token") com `&` nao escapado.
  - *Correcao:* Escapamento para `&amp;` em todas as tags de caminhos do plist.
- **7. Falso negativo em `_conflicts_with_config_dir` (`wizard.py`)**:
  - *Defeito:* Em caso de `OSError` de permissao, a funcao retornava `False` (sem conflito), permitindo sobreposicoes destrutivas de diretorios.
  - *Correcao:* Tratamento estrito de excecoes de I/O com bloqueio defensivo.
- **8. `vault_is_inside_config_dir` retornava `False` no escuro (`installer.py`)**:
  - *Defeito:* Falhas na resolucao de caminhos faziam o desinstalador achar que o vault nao estava na pasta de configuracao, abrindo risco de apagar notas de usuarios no uninstall.
  - *Correcao:* Resolucao segura e estrita com fallback conservador.
- **9. `is_up(wait_seconds=15)` checava desligamento com logica invertida (`service.py`)**:
  - *Defeito:* O metodo esperava o servico subir quando o objetivo do chamador era esperar o daemon descer apos um stop.
  - *Correcao:* Criacao do metodo explicito `wait_down(wait_seconds)`.
- **10. Contradicao em `venv_pending["detail"]` (`installer.py`)**:
  - *Defeito:* O relatorio de integridade marcava o ambiente como pronto enquanto a mensagem de detalhe afirmava pendencia de compilacao.
  - *Correcao:* Sincronizacao dos estados booleanos e textuais.
- **14. Falha de codificacao na escrita da unit no wizard (`wizard.py`)**:
  - *Defeito:* `wizard.py` gravava a unit sem `encoding="utf-8"`, levantando `UnicodeEncodeError` sob locales restritos (`LC_ALL=C`).
  - *Correcao:* Inclusao explicita de `encoding="utf-8"` na escrita de arquivos de servico.

#### Grupo 3: Integridade do Vault, YAML e Atomicidade de I/O
- **11. `_merge_alias` inseria aliases crus no frontmatter (`notes.py`)**:
  - *Defeito:* Aliases com virgula dividiam um alias em dois; aliases com colchetes ou dois-pontos corrompiam o bloco YAML.
  - *Correcao:* Sanitizacao de cada alias individualmente atraves de `yaml_quote_scalar`.
- **12. Notas com frontmatter corrompido reportavam `needs_repair: 0` (`vault.py`)**:
  - *Defeito:* Falhas no parser YAML eram suprimidas silenciosamente e a nota nao entrava na lista de reparo.
  - *Correcao:* Identificacao de erros de sintaxe YAML e sinalizacao explicita de necessidade de reparo.
- **13. Concorrencia no hook de rebuild do grafo (`graph_hook_rebuild.py`)**:
  - *Defeito:* Multiplos commits simultaneos disparavam rebuilds concorrentes que colidiam no mesmo lock.
  - *Correcao:* Implementacao de lock nao bloqueante com descarte de execucoes redundantes.
- **16. `config.json` gravado sem atomicidade (`config.py`)**:
  - *Defeito:* A configuracao era sobrescrita diretamente; se o processo caisse no meio, o arquivo ficava truncado/zerado.
  - *Correcao:* Gravacao atomica via arquivo temporario (`.tmp`) seguido de `fsync` e `os.replace`.
- **17. `.chroma_index.json` sem gravacao atomica (`vault.py`)**:
  - *Defeito:* Arquivo com milhares de carimbos mtime corria risco de corrupcao e ignorava falhas com `except Exception: pass`.
  - *Correcao:* Gravacao atomica com propagacao e tratamento correto de erros de I/O.
- **18. `_clear_previous_filing` nao validava contencao no vault (`graphbridge.py`)**:
  - *Defeito:* A funcao de remocao de notas antigas usava apenas `is_file()`, permitindo caminhos relativos malformados fora da raiz do vault.
  - *Correcao:* Validacao estrita com `resolve_in_vault()` antes de qualquer `unlink`.
- **19. Diretorio `sessions` em minusculo no hook (`session_export.py`)**:
  - *Defeito:* O hook procurava `sessions` em vez da pasta canonica `Sessions`, jogando resumos semanais na raiz do vault.
  - *Correcao:* Normalizacao do caminho para a pasta correta.
- **20. Arquivos na raiz invisiveis as metricas de saude (`vault.py`)**:
  - *Defeito:* Notas geradas na raiz do vault nao eram computadas no total de notas pelo `vault_health`.
  - *Correcao:* Inclusao de notas na raiz do vault na contagem e validacao de saude.
- **24. `session_start_brief` atribuia notas de ferramentas a humanos (`session.py`)**:
  - *Defeito:* Um passe de `relink` alterava o mtime de centenas de notas, e o brief inicial as listava como "trabalho feito por outros clientes em sessoes recentes".
  - *Correcao:* Filtragem de notas modificadas por processos automaticos do sistema.
- **29. Dessincronizacao entre cache de saude e calculo em tempo real (`vault.py`)**:
  - *Defeito:* `vault_health.json` retornava metricas desatualizadas em relacao ao `vault_health_detail()`.
  - *Correcao:* Invalidacao atomica e recalculo do cache sob demanda.
- **30. Vault desmontado respondia como vazio saudavel (`vault.py`)**:
  - *Defeito:* Se a pasta do vault estivesse inacessivel ou desmontada, o status reportava 0 notas e 0 erros sem alertar falha.
  - *Correcao:* Verificacao explicita de existencia e acessibilidade do diretorio raiz.
- **32. `yaml_quote_scalar` quebrava com caracteres de controle (`notes.py`)**:
  - *Defeito:* Bytes de controle invisiveis (0 a 31, como ``) nao eram escapados e quebravam o `yaml.safe_load`.
  - *Correcao:* Escapamento completo de caracteres de controle Unicode e ASCII.
- **33. `resolve_in_vault` levantava erro 500 com byte nulo (`notes.py`)**:
  - *Defeito:* Passar `%00` na URL causava `ValueError: embedded null byte` no sistema de arquivos em vez de retornar `None`.
  - *Correcao:* Sanitizacao de strings contra bytes nulos antes de chamar o `Path`.
- **37. `repair._ja_tratada` sobrescrevia notas ilegiveis (`organizer.py`)**:
  - *Defeito:* Quando a funcao nao conseguia ler a nota por erro de I/O, retornava `False` e o reparo tentava sobrescrever o arquivo cego.
  - *Correcao:* Retorno seguro de `True` (nao mexer) quando a leitura falhar.

#### Grupo 4: Ingestao, Extracao e Processos
- **15. Extracao de `.groovy` corrompia caracteres acentuados (`extract.py`)**:
  - *Defeito:* Arquivos eram lidos com `errors="replace"` sem encoding explicito, convertendo cedilhas e acentos em caracteres de substituicao `�`.
  - *Correcao:* Abertura com deteccao e fallback explicito de UTF-8.
- **26. Piso de substancia ausente para arquivos nao binarios (`extractor.py`)**:
  - *Defeito:* O limite minimo de texto cobria PDFs e DOCX, mas ignorava `.html`, `.json` e `.txt`. Um HTML sem texto gerava uma nota vazia com alucinacoes de pessoas/decisoes inventadas pelo modelo.
  - *Correcao:* Aplicacao do piso de substancia para todos os tipos MIME suportados.
- **27. Afirmacoes manuais desatualizadas em `capabilities()` (`capabilities.py`)**:
  - *Defeito:* O dicionario `known_unwired` continha ferramentas que ja haviam sido integradas ou removidas.
  - *Correcao:* Sincronizacao do registro de capacidades com a inspecao estatica do AST.
- **31. `ProcessTracker` quebrava com tipo raiz incorreto (`tracker.py`)**:
  - *Defeito:* Receber um JSON valido com formato `{}` em vez de `[]` levantava `AttributeError` no append.
  - *Correcao:* Validacao de `isinstance(data, list)` com recuperacao graciosa.
- **34. Divergencia sobre o escopo padrao de busca (`server.py`)**:
  - *Defeito:* Docstrings e relatorios afirmavam escopos fixos conflitantes enquanto a implementacao e adaptativa.
  - *Correcao:* Unificacao da documentacao e retorno explicito do escopo resolvido na resposta da busca.
- **35. Arquivos ignorados tratados incorretamente no inbox (`organizer.py`)**:
  - *Defeito:* Arquivos `LICENSE` iam para `skipped` e permaneciam no inbox, disparando loops de manutencao a cada nova sessao.
  - *Correcao:* Mapeamento correto de arquivos boilerplate para descarte seguro em `junk`.
- **36. `ingest._load_registry` sem validacao de tipo raiz (`ingest.py`)**:
  - *Defeito:* Se o arquivo de fontes indexadas contivesse uma lista em vez de dicionario, o carregador quebrava com `TypeError`.
  - *Correcao:* Validacao com `isinstance(data, dict)` antes de indexar caminhos.
- **40. `count_words` cacheava falhas transitorias como 0 palavras (`extractor.py`)**:
  - *Defeito:* Uma excecao transitoria de I/O gravava `0` palavras no cache `(size, mtime_ns)`, impedindo reindexacoes futuras do arquivo.
  - *Correcao:* Erros de leitura nao sao gravados no cache persistente.

#### Grupo 5: Grafos de Codigo e Resolucao de Imports (`graph/`)
- **38. Nao determinismo no desempate de nos (`_pick_winner`) (`dedup.py`)**:
  - *Defeito:* Quando IDs tinham o mesmo sufixo e comprimento, o desempate caia na posicao da lista no chunk, gerando grafos diferentes a cada execucao.
  - *Correcao:* Desempate deterministico pelo identificador completo do no.
- **39. Rotulos repetidos no fallback de nomes curtos (`build.py`)**:
  - *Defeito:* `_shortest_unique_suffix` devolvia basenames duplicados para arquivos de mesmo nome em pastas distintas.
  - *Correcao:* Extensao progressiva de diretorios pais ate a garantia de unicidade visual.
- **41. Agregacao de dependencias ausentes por texto fragil (`extract.py`)**:
  - *Defeito:* Casamento de string crua ("not installed" in erro) falhava em mensagens dinamicas de erro de import de parsers.
  - *Correcao:* Captura tipada de `ImportError` e `ModuleNotFoundError`.
- **42. `_strip_jsonc` corrompia strings contendo virgulas (`resolution.py`)**:
  - *Defeito:* A remocao de virgulas finais (trailing commas) via regex cru sobre o arquivo alterava valores literais em strings JSON.
  - *Correcao:* Parser ciente de strings literais antes da aplicacao do filtro de comentarios e virgulas.
- **43. Resolucao de curingas retornava asterisco cru (`resolution.py`)**:
  - *Defeito:* Em aliases como `@lib/*.css`, se a captura do curinga fosse vazia (`@lib/.css`), a string voltava com o asterisco intacto (`estilos/*.css`).
  - *Correcao:* Tratamento explicito de capturas de tamanho zero.
- **44. `_resolve_js_import_path` confundia diretorios com arquivos (`resolution.py`)**:
  - *Defeito:* Ao importar `./comp`, se `src/comp/` existisse como pasta, o resolvedor entregava a pasta como se fosse o modulo final em vez de procurar `src/comp/index.ts`.
  - *Correcao:* Verificacao explicita de `is_dir()` e busca em cascata de arquivos index.
- **45. Listas inline no `pnpm-workspace.yaml` eram ignoradas (`resolution.py`)**:
  - *Defeito:* O parser manual de YAML aceitava apenas listas em bloco com hifens; a sintaxe inline `packages: ['apps/*', 'libs/*']` retornava lista vazia.
  - *Correcao:* Suporte a listas inline entre colchetes e aspas.
- **46. Tipos invalidos em `tsconfig.json` descartavam arquivos inteiros (`resolution.py`)**:
  - *Defeito:* 9 variacoes de valores nulos ou tipos errados em `compilerOptions`, `paths` e `baseUrl` causavam `AttributeError`/`TypeError` e abortavam a extracao do arquivo todo.
  - *Correcao:* Validacao defensiva tipo a tipo com degradacao graciosa por alias individual.
- **47. Inclusoes `#include` em C permitiam fuga do repositorio (`resolution.py`)**:
  - *Defeito:* `#include "/etc/passwd"` descartava o diretorio base e resolvia o arquivo absoluto no sistema operacional.
  - *Correcao:* Bloqueio de caminhos absolutos e contencao no diretorio do projeto.
- **48. `require()` em Lua com ponto inicial sondava a raiz do disco (`resolution.py`)**:
  - *Defeito:* A substituicao de `.` por `/` transformava `require(".tmp.segredo")` em `/tmp/segredo.lua`, descartando o diretorio base.
  - *Correcao:* Tratamento de pontos iniciais e sanitizacao antes da conversao de separadores.
- **49. Travessia ascendente em imports relativos com escopo de raiz (`extract.py` e `resolution.py`)**:
  - *Defeito:* Imports relativos como `import "../../fora"` resolviam para nos fora do repositorio varrido porque os analisadores de AST usam assinaturas fixas sem receber a raiz do scan.
  - *Correcao:* Criacao do context manager `escopo_de_root(root)` e aplicacao de `_dentro_do_root` em `extract.py` e nos resolvedores de JS/TS/C/Lua, contendo alvos relativos na raiz do projeto.

---

## v0.13.0-rc1 (2026-08-23) - Client Scoping and Code Graph Consolidation

### Added
- **Client Metadata Scoping (`search_vault(client=...)`)**:
  - Promoted `client:` frontmatter field to indexed ChromaDB metadata.
  - Added client-filtered search composed via `$and` logic with scope filters, supporting path-based client derivation for ingested files.
- **Code Graph Ingestion Engine (Graphify Integration)**:
  - Vendored and adapted Graphify AST pipeline supporting Python, JavaScript, TypeScript, Rust, Go, and C/C++.
  - Added sourcemaps extraction (`extract_source_maps`), code blast-radius analysis (`graph_affected`), and automated git commit hooks (`graph_hook_install`).

---

## v0.12.3 (2026-08-23) - Service Stop Timeouts and ChromaDB Hardening

### Fixed
- **Service Manager Daemon Stop Timeout**:
  - Set `TimeoutStopSec=600` on generated systemd user units and `ExitTimeOut` on launchd plists.
  - Prevented OS service managers from sending SIGKILL mid-write to ChromaDB and SQLite during lengthy reindexes or relinking passes, resolving HNSW segment corruption.

---

## v0.12.2 (2026-08-23) - Note Linking Resolvers and Background Relinking

### Added
- **`relink_folder_bg` Background Relinking**:
  - Asynchronous background execution for heavy folder cross-linking, preventing client timeout drops on large folders.

### Fixed
- **Date-Stripped Wikilink Resolution**:
  - Allowed `[[Title]]` to resolve against `{date}-{title}.md` without forcing users or agents to type date prefixes in links.
- **Safe Filename Truncation Aliasing**:
  - Emitted untruncated title aliases in note frontmatter when filename truncation occurred.

---

## v0.12.1 (2026-08-23) - Field Defect Resolutions and Data Safety

### Fixed
- **Legacy Collection Adoption**:
  - Gated adoption of legacy vector collections on measured embedding dimensions, preventing dimensional mismatches between bge-base (768-dim) and bge-m3 (1024-dim).
- **Incremental mtime Re-Ingestion**:
  - Fixed incremental reindex to only force re-embeds for files whose index rows are actually missing.
- **Stale Vector Tail Cleanup**:
  - Fixed `upsert` so shortened notes remove excess chunks rather than leaving stale text fragments in the vector index.
- **Dataless File Detection**:
  - Prevented cloud-evicted or zero-block ext4/btrfs files from indexing corrupt empty content.
- **Scope Defaulting**:
  - Handled default search scope dynamically based on vault composition.

---

## v0.12.0 (2026-08-20) - Note Chunking and Execution Limits

### Added
- **Vault Note Chunking**:
  - Chunked long vault notes during embedding to match the token context limits of embedding models.
- **Orphan Sweep Hardening**:
  - Verified and hardened orphan note classification.

---

## v0.11.0 to v0.11.4 (2026-08-12 to 2026-08-14) - Single HTTP Daemon Architecture & Web Dashboard

### Added
- **Single HTTP Daemon Architecture (`127.0.0.1:8787/mcp`)**:
  - Major architectural pivot from multi-process stdio to a single long-lived FastMCP HTTP daemon.
  - Eliminated VRAM duplication of `bge-m3` across multiple connected MCP clients.
  - Added Bearer token authentication on loopback.
- **Integrated Web Dashboard API (`dashboard_api.py`)**:
  - Served dashboard endpoints (`/api/status`, `/api/vault/tree`, `/api/vault/graph`, `/api/processes`, `/api/llama`) directly from the daemon on port 8788.
- **Multi-Agent Local Queue (`local_task_submit`, `local_task_status`)**:
  - Shared asynchronous task queue allowing multiple AI agents to submit background jobs to the local LLM.

---

## v0.10.0 (2026-08-03) - Note Renaming, Graph Hubs, and Literal Search

### Added
- **Atomic Note Renaming (`vault_rename_note` / `POST /api/vault/note/rename`)**:
  - Staged, atomic renaming of notes with automated repointing of all inbound `[[wikilinks]]`.
- **Community Hub Labeling (`graph/cluster.py`)**:
  - Named graph communities using dominant locally-defined code symbols rather than generic identifiers.
- **Paced Task Status Reporting (`task_status`)**:
  - Added execution metrics and typical run durations based on persisted job history (`job_durations.json`).
- **Literal Note Search (`vault_find_notes`)**:
  - Exact stem, prefix, and substring title matching bypassing embedding cutoffs.
- **Backlinks Panel (`vault_note_links`)**:
  - Detailed inbound and outbound link graph reporting with broken link flags.

---

## v0.7.0 to v0.9.0 (2026-07-20 to 2026-07-28) - Engine Modes, Llama Controls, and Task Tracker

### Added
- **Tri-Mode Engine Architecture (`engine_mode`)**:
  - `local`: synthesis and summarization run offline on local llama.cpp.
  - `agent`: zero local LLM overhead; synthesis delegated to calling agent.
  - `hybrid`: interactive work handled by agent; maintenance and bulk synthesis handled locally.
- **Dashboard Task Tracker Panel**:
  - Added process tracking UI in dashboard and start/stop button for llama-server.
- **Opt-in Web Search**:
  - Privacy-preserving DuckDuckGo search integration via `ddgs`.
- **Graph Exporters (`graph_export`)**:
  - Export code knowledge graphs to GraphML (Gephi/Cytoscape), SVG, and Cypher (Neo4j).

---

## v0.5.0 to v0.6.4 (2026-06-29 to 2026-07-10) - Ingestion, Semantic Relinking, and Multiplatform Downloader

### Added
- **Recursive Folder Ingest (`ingest_folder` / `ingest_folder_bg`)**:
  - Extraction and vector indexing of PDF, DOCX, XLSX, PPTX, HTML, and MDX files.
- **Semantic Note Relinking (`relink_folder`)**:
  - Automated cross-linking of unlinked notes based on embedding similarity thresholds.
- **Multiplatform Llama.cpp Downloader (`src/delegation_core/downloader.py`)**:
  - Automated release asset discovery and extraction for Linux (`.tar.gz`), macOS (`.tar.gz`), and Windows (`.zip`), preserving dynamic library symlinks.
- **Bundled Agent Skills (`skills/`)**:
  - Integrated 17 universal Claude Code agent skills deployed into `~/.claude/skills/`.

### Fixed
- **YAML Frontmatter Colon Quoting**:
  - Sanitized and quoted frontmatter scalars containing colons and special characters.
- **Reasoning Tag Stripping**:
  - Added `_strip_think_tags()` to purge chain-of-thought `<think>` blocks from frontmatter.

---

## v0.1.0 to v0.4.0 (2026-06-10 to 2026-06-27) - Inception: Local Memory Bridge & Vault Vector Store

### Added
- **Initial MCP Stdio Memory Server**:
  - Inception of `delegation-core` as an offline memory bridge connecting AI agents to Obsidian vaults.
  - Semantic vector search using `BAAI/bge-m3` embeddings stored in ChromaDB.
- **Core MCP Tools**:
  - `search_vault`: semantic vector query against notes.
  - `read_note` / `write_note`: curated markdown reading and atomic creation with frontmatter.
  - `compress`: automatic summarization of lengthy session notes.
  - `vault_health_detail` & `vault_stats`: orphan detection, unindexed tracking, and broken wikilink accounting.
  - `run_maintenance`: automated healing of broken links and metadata.
