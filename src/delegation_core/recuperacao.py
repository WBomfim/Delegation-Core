"""
recuperacao.py: o indice que derruba quem o abre se conserta sozinho.

Existe por causa de dois Macs em 29/09/2026. Nos dois o indice passou a matar
com SIGSEGV, dentro de `chromadb_rust_bindings`, todo processo que o abria. O
launchd reiniciava o daemon, o daemon novo abria o indice e caia de novo: 5
partidas num dia num Mac, e em nenhuma delas algo dizia o que fazer. O conserto
que o `doctor` recomendava era manual (guardar o indice, reconstruir num caminho
limpo, refazer cada ingestao a mao), e `reindex --force` caia do mesmo jeito,
porque abre o mesmo caminho.

O que este modulo faz, na ordem:

1. `antes_de_abrir` deixa um marcador com o PID de quem vai abrir o indice, e
   `depois_de_abrir` o remove. Um marcador de um PID que ja nao existe quer
   dizer que aquele processo morreu no meio da abertura.
2. Nesse caso, antes de abrir, o indice e testado num processo FILHO (a mesma
   sonda do `doctor`). Se o filho tambem nao abre, morto por sinal ou parado
   ate o prazo, o dano esta confirmado por duas falhas independentes, e o
   indice vai para quarentena: renomeado ao lado, nunca apagado. Se o filho
   abre normalmente, a morte anterior teve outra causa (OOM, kill manual) e
   nada muda. Travar conta como morrer: um indice cujo `count()` nunca volta
   faz o watchdog do loop encerrar o daemon, e o ciclo e o mesmo do SIGSEGV.
3. Na quarentena os carimbos que certificavam o indice antigo sao zerados, os
   do vault e os de cada fonte ingerida, porque certificam linhas que o indice
   novo nao tem. Sem isso o reindex "incremental" pularia todas as notas e o
   indice novo nasceria vazio e com cara de saudavel.
4. Fica gravado um pedido de reconstrucao. O daemon, ao subir, executa: reindex
   completo das notas e reingestao de cada fonte registrada. O pedido guarda o
   progresso, entao um daemon que cai no meio continua de onde parou.

Um vault numa pasta sincronizada (OneDrive, iCloud, Dropbox, Google Drive) tem
o indice dentro dela, e sincronizacao mexendo em SQLite e em HNSW e causa
conhecida de dano. Um dos dois Macs era esse caso. Quando a quarentena acontece
num indice assim, o novo ja nasce fora da pasta sincronizada, em `index_path`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

from . import config as _config
from .config import (  # noqa: F401  (moram no config; reexportados)
    _MARCAS_DE_NUVEM,
    caminho_local_do_indice,
    em_pasta_sincronizada,
)

logger = logging.getLogger("recuperacao")

def _dir_de_estado() -> Path:
    # Lido na hora, e nao importado: o conftest da suite reaponta
    # config.CONFIG_DIR, e uma copia feita no import escaparia dele.
    return Path(_config.CONFIG_DIR)


def caminho_do_marcador() -> Path:
    return _dir_de_estado() / "abertura_do_indice.json"


def caminho_do_pedido() -> Path:
    return _dir_de_estado() / "reconstrucao_do_indice.json"


def _ler_json(caminho: Path) -> dict | None:
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("%s ilegivel (%s), ignorado", caminho.name, e)
        return None
    return dados if isinstance(dados, dict) else None


def _gravar_json(caminho: Path, dados: dict) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(caminho.parent), prefix=caminho.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(dados, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, caminho)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _pid_vivo(pid: int) -> bool:
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:  # pragma: no cover - psutil e dependencia declarada
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


# ── marcador de abertura ─────────────────────────────────────────────────────

def antes_de_abrir(cfg) -> bool:
    """Chamado imediatamente antes de abrir o indice. True se pos em quarentena.

    Nunca levanta: uma falha aqui nao pode ser o motivo de o indice nao abrir.
    """
    try:
        marcador = _ler_json(caminho_do_marcador())
        caminho = str(cfg.chroma_path)
        quarentena = False
        if (marcador and marcador.get("path") == caminho
                and marcador.get("pid") != os.getpid()
                and not _pid_vivo(int(marcador.get("pid") or 0))):
            logger.warning(
                "O processo %s morreu enquanto abria o indice em %s (desde %s). "
                "Testando o indice num processo filho antes de abrir.",
                marcador.get("pid"), caminho, marcador.get("inicio"))
            falha = falha_da_sonda(cfg)
            if falha:
                pos_em_quarentena(cfg, motivo=(
                    f"o processo {marcador.get('pid')} morreu abrindo o indice e "
                    f"a sonda num processo filho tambem nao abriu ({falha})"))
                quarentena = True
            else:
                logger.info("A sonda abriu o indice normalmente: a morte anterior "
                            "teve outra causa, o indice fica como esta")
        _gravar_json(caminho_do_marcador(), {
            "pid": os.getpid(), "path": str(cfg.chroma_path),
            "inicio": datetime.now().isoformat(timespec="seconds")})
        return quarentena
    except Exception as e:
        logger.warning("Guarda de abertura do indice falhou (%s); abrindo sem ela", e)
        return False


def depois_de_abrir(cfg) -> None:
    """O indice abriu e respondeu: a abertura deste processo nao e mais suspeita."""
    try:
        marcador = _ler_json(caminho_do_marcador())
        if marcador and marcador.get("pid") == os.getpid():
            caminho_do_marcador().unlink(missing_ok=True)
    except Exception as e:
        logger.warning("Nao consegui remover o marcador de abertura: %s", e)


#: Quanto a sonda da guarda espera o indice abrir. Maior que os 120s do doctor
#: porque aqui um tempo esgotado confirma dano, e a margem protege o indice
#: grande em disco lento de uma quarentena por engano.
PRAZO_DA_SONDA = 180


def falha_da_sonda(cfg) -> str | None:
    """A sonda do doctor, num filho, conseguiu abrir e consultar o indice?

    None quando conseguiu. Senao, o que aconteceu: morte por sinal ou prazo
    esgotado. As duas sao a mesma condicao vista de lados diferentes, e as duas
    foram medidas em campo em 29/09/2026:

    - num Mac o indice matava quem o abria com SIGSEGV;
    - noutro, `collection.count()` nunca voltava, com threads paradas em
      mutexwait dentro de chromadb_rust_bindings, e o watchdog do loop
      encerrava o daemon a cada 300s. A primeira versao desta guarda so
      aceitava sinal, tomou o prazo esgotado por indice saudavel, e o ciclo
      continuou.

    Erro de Python com saida normal nao conta: o `_init` ja o trata tentando
    de novo na chamada seguinte, sem derrubar o processo.
    """
    from .doctor import sondar_indice
    sonda = sondar_indice(cfg, timeout=PRAZO_DA_SONDA)
    if sonda.get("sinal") is not None:
        return f"morreu pelo sinal {sonda['sinal']}"
    if sonda.get("timeout"):
        return f"nao abriu em {PRAZO_DA_SONDA}s"
    return None


# ── quarentena ───────────────────────────────────────────────────────────────

def pos_em_quarentena(cfg, motivo: str, novo_caminho: str | None = None) -> dict:
    """Tira o indice do caminho, zera os carimbos e pede a reconstrucao.

    O indice antigo e RENOMEADO ao lado, na mesma pasta: e instantaneo em
    qualquer tamanho, nao precisa de espaco livre, e nada e apagado. Remover o
    diretorio em quarentena fica para quem conferir que o novo esta bom. Dentro
    do vault, `delegation-core doctor --clean-orphans` o encontra; fora dele, o
    caminho esta no pedido e no heartbeat.

    `novo_caminho` manda o indice novo para outro lugar e grava `index_path`.
    Sem ele, um indice numa pasta sincronizada vai para `caminho_local_do_indice`.
    """
    from . import ingest as _ingest
    from . import notes

    antigo = Path(cfg.chroma_path)
    selo = datetime.now().strftime("%Y%m%d-%H%M%S")
    destino = None
    if antigo.exists():
        destino = antigo.with_name(f"{antigo.name}-danificado-{selo}")
        os.rename(antigo, destino)
        logger.warning("Indice posto em quarentena: %s -> %s (%s)", antigo, destino, motivo)

    relocado = None
    if novo_caminho:
        relocado = Path(novo_caminho)
    elif not getattr(cfg, "index_path", "") and em_pasta_sincronizada(antigo):
        relocado = caminho_local_do_indice()
    if relocado is not None and relocado != antigo:
        cfg.index_path = str(relocado)
        try:
            cfg.save()
        except Exception as e:
            logger.warning("Nao consegui gravar index_path na config (%s); o indice "
                           "novo fica em %s so ate o daemon reiniciar", e, relocado)
        logger.warning("O vault esta numa pasta sincronizada; o indice novo vai "
                       "para %s, fora dela", relocado)

    # Carimbos do vault: certificavam linhas do indice antigo.
    notes.atualizar_estado(notes.caminho_do_estado(cfg.vault), lambda _estado: {})

    # Carimbos por arquivo de cada fonte ingerida, pelo mesmo motivo. As
    # configuracoes da fonte (recursive, exclude) ficam: sao elas que dizem
    # como reingerir.
    # A lista de fontes vem do registro E do indice que acabou de sair. So o
    # registro nao basta: em 29/09/2026 um teste o tinha reduzido a uma fonte do
    # pytest num Mac, e a reconstrucao de la pulou as 172 fontes reais. O indice
    # guarda `source_folder` em cada linha externa e le por sqlite puro, mesmo
    # quando trava ou derruba o chromadb. Se nem isso ler, fica o registro.
    registro = _ingest._load_registry()
    if destino is not None:
        try:
            registro = _ingest.reconstruir_registro_do_indice(destino, carimbar_arquivos=False)
        except Exception as e:
            logger.warning("Nao consegui ler as fontes do indice em quarentena (%s); "
                           "a reconstrucao usa so o registro", e)
    fontes = []
    for fonte, entrada in registro.items():
        if not isinstance(entrada, dict):
            entrada = {}
        fontes.append({"path": fonte,
                       "recursive": bool(entrada.get("recursive", True)),
                       "exclude": entrada.get("exclude") or []})
        entrada["files"] = {}
        registro[fonte] = entrada
    if registro:
        _ingest._save_registry(registro)

    pedido = {
        "motivo": motivo,
        "pedido_em": datetime.now().isoformat(timespec="seconds"),
        "quarentena": str(destino) if destino else None,
        "indice_novo": str(cfg.chroma_path),
        "relocado_para_fora_da_nuvem": bool(relocado),
        "notas_feitas": False,
        "fontes": fontes,
        "fontes_feitas": [],
    }
    _gravar_json(caminho_do_pedido(), pedido)
    with contextlib.suppress(OSError):
        caminho_do_marcador().unlink(missing_ok=True)
    return pedido


# ── reconstrucao ─────────────────────────────────────────────────────────────

def reconstrucao_pendente() -> dict | None:
    return _ler_json(caminho_do_pedido())


def reconstruir(vault, ingest) -> dict:
    """Refaz o indice a partir das fontes: as notas do vault e cada ingestao.

    Idempotente e retomavel: cada etapa concluida fica gravada no pedido, e um
    daemon que cai no meio recomeca da proxima etapa, nao do zero.
    """
    pedido = reconstrucao_pendente()
    if not pedido:
        return {"status": "nada a reconstruir"}

    # `_ensure_ready` nao levanta quando a abertura falha: deixa `collection`
    # vazia e tenta de novo na proxima chamada. Seguir assim marcaria as notas
    # como feitas num indice que nao abriu, e o pedido se apagaria no fim.
    vault._ensure_ready()
    if getattr(vault, "collection", None) is None:
        raise RuntimeError("o indice novo nao abriu; o pedido de reconstrucao "
                           "fica gravado e e retomado na proxima partida")

    resultado = {"notas": None, "fontes": []}
    if not pedido.get("notas_feitas"):
        resultado["notas"] = vault.reindex_vault(force=True)
        pedido["notas_feitas"] = True
        _gravar_json(caminho_do_pedido(), pedido)

    feitas = set(pedido.get("fontes_feitas") or [])
    for fonte in pedido.get("fontes") or []:
        caminho = fonte.get("path")
        if not caminho or caminho in feitas:
            continue
        if not Path(caminho).exists():
            saida = {"source": caminho, "error": "fonte nao existe mais; pulada"}
        else:
            saida = ingest.ingest(caminho, recursive=fonte.get("recursive", True),
                                  force=True, exclude=fonte.get("exclude") or None)
        resultado["fontes"].append(saida)
        feitas.add(caminho)
        pedido["fontes_feitas"] = sorted(feitas)
        _gravar_json(caminho_do_pedido(), pedido)

    caminho_do_pedido().unlink(missing_ok=True)
    resultado["status"] = "reconstruido"
    resultado["quarentena"] = pedido.get("quarentena")
    logger.info("Reconstrucao do indice concluida: %s notas, %s fontes",
                resultado["notas"], len(resultado["fontes"]))
    return resultado
