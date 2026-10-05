# Instalação no Mac com MLX, integrada ao delegation-core

Instruções para o Claude Code rodando no MacBook Pro M5 Max (48 GB). Leia tudo antes
de começar. Trabalhe etapa por etapa e só avance quando a verificação da etapa passar.
Se uma verificação falhar, pare e relate o que viu: não improvise correção no código
do delegation-core nesta máquina (ver "Regras" no fim).

## Objetivo

1. Rodar permanentemente, em modo headless, o Qwen3.8 27B em MLX 8-bit, servido pelo
   `mlx_lm.server` numa API compatível com OpenAI.
2. Instalar o delegation-core e fazer ele usar esse servidor como motor local, no lugar
   do llama.cpp. O MLX vira o serviço permanente; o delegation-core só conversa com ele.
3. Registrar o delegation-core como servidor MCP no Claude Code desta máquina.

## Como a integração funciona (leia antes de mexer)

Conferido no código de produção (`src/delegation_core/engine.py`) em 2026-09-28:

- O delegation-core fala com o motor em `http://localhost:{llama_port}`. Usa só dois
  endpoints: `GET /health` e `POST /v1/chat/completions`.
- Antes de cada chamada ele testa `/health`. Se responder 200, usa o servidor que já
  está no ar e **nunca tenta subir outro**. Só quando `/health` falha ele tenta lançar
  `llama_binary` com flags do llama.cpp (`--ctx-size`, `-fa`, `-ctk`), que o
  `mlx_lm.server` não aceita. Ou seja: com o MLX no ar, tudo funciona; com o MLX fora,
  o delegation-core falha com erro no log em vez de consertar sozinho. É o esperado.
- O desligamento por ociosidade (`local_idle_shutdown_sec`) só mata processo que o
  próprio delegation-core iniciou. O serviço do MLX nunca é tocado.
- Toda requisição manda `"model": "local"` (valor fixo no código) e
  `"chat_template_kwargs": {"enable_thinking": false}`. **Esses dois campos são os
  pontos de risco**: a etapa 6 testa se o `mlx_lm.server` aceita os dois.

Porta: use **8181**, que é o `llama_port` padrão do delegation-core. Assim a config não
precisa de porta diferente e não há dois lugares para manter sincronizados.

## 1. Pré-requisitos

- `uname -m` deve dar `arm64`. Anote a versão do macOS (`sw_vers`).
- Instale o `uv` se não existir (`brew install uv`) e o CLI do Hugging Face
  (`uv tool install "huggingface_hub[cli]"`).
- Confirme `git` e Python 3.12 disponíveis (o `uv` resolve o Python se faltar).

## 2. Ambiente MLX

- Crie o venv em `~/mlx-server` com Python 3.12: `uv venv --python 3.12 ~/mlx-server/.venv`.
- Instale `mlx-lm` (versão mais recente) nesse venv.
- Confirme que o `mlx-lm` instalado suporta a arquitetura do modelo (procure o módulo
  correspondente em `mlx_lm/models/`, ex. `qwen3_5`). Se não suportar, instale do
  GitHub (`ml-explore/mlx-lm`, branch main).
- Rode `~/mlx-server/.venv/bin/mlx_lm.server --help` e anote se existem as opções
  `--chat-template-args` e `--max-tokens`. A etapa 5 depende disso.

## 3. Modelo

- Pesquise no Hugging Face um repositório MLX **8-bit** do `Qwen/Qwen3.8-27B`
  (priorize `mlx-community`, depois o mais baixado). **Mostre o nome ao usuário antes
  de baixar** e espere confirmação.
- Baixe com `hf download <repo>`. Anote o caminho do snapshot no cache
  (`~/.cache/huggingface/hub/models--.../snapshots/<hash>`), ele vai para a config
  do delegation-core.
- Teste rápido com `mlx_lm.generate --model <repo> --prompt "..."` e confirme que a
  resposta é coerente.

## 4. Limite de memória da GPU, persistente

Orçamento: pesos 8-bit de um 27B ocupam perto de 28 a 30 GB. O BGE-M3 do
delegation-core roda no mesmo chip (via `mps`) e ocupa mais uns 2 GB. Por isso o alvo
de 40 GB para a GPU, deixando cerca de 8 GB para o macOS.

- Crie `/Library/LaunchDaemons/com.local.gpu-wired-limit.plist` que rode
  `/usr/sbin/sysctl -w iogpu.wired_limit_mb=40960`, com `RunAtLoad=true`, dono
  `root:wheel`, permissão 644.
- Carregue com `sudo launchctl bootstrap system <plist>` e confirme com
  `sysctl iogpu.wired_limit_mb`.
- Todo comando com `sudo`: se você não conseguir rodar, mostre o comando exato para o
  usuário executar com `! <comando>` e espere o resultado.

## 5. Servidor MLX como serviço permanente

- Crie `~/Library/LaunchAgents/com.local.mlx-server.plist` que execute:

  ```
  ~/mlx-server/.venv/bin/mlx_lm.server --model <repo> --host 127.0.0.1 --port 8181
  ```

  Se a etapa 2 mostrou `--chat-template-args`, acrescente
  `--chat-template-args '{"enable_thinking": false}'` (no plist, cada argumento é um
  `<string>` separado, sem aspas de shell). Isso garante resposta sem bloco de
  raciocínio mesmo que o servidor ignore o campo enviado pelo delegation-core.
- No plist use caminhos absolutos (`/Users/<usuario>/...`), o launchd não expande `~`.
- `RunAtLoad=true`, `KeepAlive=true`, logs em `~/mlx-server/logs/stdout.log` e
  `stderr.log` (crie a pasta antes).
- Carregue com `launchctl bootstrap gui/$(id -u) <plist>`.
- Verifique: `curl -s http://127.0.0.1:8181/v1/models`.

## 6. Teste de compatibilidade com o delegation-core (decisivo)

Rode as três chamadas abaixo e mostre o resultado de cada uma ao usuário. Não siga
para a etapa 7 sem as três passando.

```bash
# a) health: o delegation-core exige HTTP 200 aqui
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8181/health

# b) exatamente o formato que o delegation-core envia
curl -s http://127.0.0.1:8181/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"local","messages":[{"role":"user","content":"Responda so: ok"}],
       "max_tokens":50,"temperature":0.0,
       "chat_template_kwargs":{"enable_thinking":false}}'

# c) a resposta de (b) precisa ter texto em choices[0].message.content,
#    sem bloco <think>, e usage.completion_tokens > 0
```

Resultados possíveis:

- **Tudo passa**: siga para a etapa 7.
- **`/health` não dá 200**: o delegation-core vai achar que o motor está fora. Pare e
  relate a versão do `mlx-lm` e o código HTTP.
- **`"model":"local"` dá erro** (o servidor tenta baixar um repositório chamado
  `local`): pare e relate a mensagem. A correção é no delegation-core (tornar o nome
  do modelo configurável), feita no repositório de produção, não aqui.
- **Resposta vem com raciocínio ou vazia**: confira se o `--chat-template-args` da
  etapa 5 foi aplicado; se não existir essa opção, relate.

## 7. Instalar o delegation-core

- Clone o repositório de produção (`AnonJoey/Delegation-Core-Office`, branch `master`) em
  `~/Projects/delegation-core`. Pergunte ao usuário o caminho se o clone falhar por
  acesso.
- Leia `README.md` e `src/delegation_core/wizard.py` antes de rodar o instalador, para
  saber o que cada pergunta faz.
- Rode `./install.command` (ou `./install.sh`).
- **Não deixe o instalador baixar llama.cpp nem modelo GGUF, e não aceite o serviço de
  inicialização do llama.cpp** (`_startup_launchd` em `wizard.py`): ele criaria um
  segundo motor disputando a porta e a memória com o MLX. Se o wizard não oferecer
  pular, escolha o modo agente nessa etapa e ajuste a config depois (próxima etapa).
- Pergunte ao usuário onde fica o vault nesta máquina antes de responder `vault_path`.

## 8. Apontar o delegation-core para o MLX

Edite `~/.delegation_core/config.json` (faça backup antes:
`cp config.json config.json.bak-pre-mlx-$(date +%Y%m%d-%H%M)`):

```json
{
  "engine_mode": "hybrid",
  "llama_port": 8181,
  "llama_binary": "/Users/<usuario>/mlx-server/.venv/bin/mlx_lm.server",
  "llama_model": "/Users/<usuario>/.cache/huggingface/hub/models--<...>/snapshots/<hash>",
  "embed_device": "mps",
  "budget_mode": "auto"
}
```

Por que cada campo:

- `llama_binary` e `llama_model` precisam apontar para arquivos que existem, senão
  `is_configured()` e o `delegation-core doctor` acusam configuração incompleta. Eles
  não são usados para subir nada enquanto o MLX responder.
- `embed_device: mps` coloca o BGE no chip. Se der erro de memória, troque por `cpu`.
- `budget_mode: auto` usa a velocidade medida (`tok_sec`) para limitar tokens por
  tarefa. Rode a calibração depois da etapa 9 para preencher `tok_sec`.
- Não mexa em `server_token`, `vault_path` nem nas pastas do vault.

## 9. Registrar no Claude Code e verificar ponta a ponta

- Confirme que o instalador registrou `delegation-core` em `~/.claude.json`
  (`mcpServers`). Se não, registre com `claude mcp add` seguindo o README.
- Copie `AGENT_GUIDE.md` para `~/.delegation_core/` se o instalador não copiou, e
  garanta que `~/.claude/CLAUDE.md` o referencia.
- Rode `delegation-core doctor` e mostre a saída.
- Reinicie o Claude Code e, numa sessão nova: `capabilities()`, `heartbeat()` (deve dar
  `healthy` com o motor online), e um `compress()` com um texto curto para provar que a
  geração passa pelo MLX. Confira no `~/mlx-server/logs/stderr.log` que a requisição
  chegou.

## 10. Evitar que o Mac durma na tomada (opcional)

`sudo pmset -c sleep 0` (só na tomada; na bateria o comportamento fica normal).

## 11. Verificação final e entrega

- Chat completion de teste com resposta de cerca de 300 tokens, medindo tok/s.
- Uso de memória durante a geração (`memory_pressure`, `top -l 1 -o mem`).
- Entregue ao usuário um resumo com: repositório do modelo, caminho dos dois plists,
  onde ficam os logs (MLX e `~/.delegation_core/server.log`), como parar, reiniciar e
  desinstalar cada serviço (`launchctl bootout ...`), resultado do teste da etapa 6 e
  o `tok_sec` medido.
- Grave um resumo da instalação no vault com `write_note(folder="Infrastructure", ...)`.

## Regras

- Nada de travessão (em dash ou en dash) em texto nenhum, inclusive notas e commits.
- Meça antes de concluir: cada "funciona" precisa de uma saída de comando que prove.
- Não altere o código do delegation-core nesta máquina. Defeito encontrado vira nota em
  `Fixes/` no vault e relato ao usuário; a correção é feita no repositório de produção.
- Antes de sobrescrever qualquer arquivo de config ou plist, faça backup e mostre o
  que vai mudar.
