"""Trava exclusiva entre processos, para ler-modificar-escrever sem perder update.

Este modulo existe por um defeito com consequencia medida, e nao por precaucao.

Em 31/08/2026 dois `ingest_folder` correram juntos nesta maquina. Os dois
carregaram `ingested_sources.json`, os dois acrescentaram a propria entrada, e o
que gravou por ultimo escreveu por cima: **as entradas de Soteria e Pessoal
sumiram do registro**. Ninguem viu na hora, porque o arquivo continuou valido e
a busca so parou de achar as duas pastas. O contorno adotado foi humano, "nunca
rode duas ingestoes ao mesmo tempo", e sobreviveu quase um mes assim.

`_atomic_write_registry` ja existia e nao resolve isto. Escrita atomica garante
que ninguem le um arquivo pela metade; ela nao garante que quem leu antes de
voce ainda esteja no que voce grava. Sao dois problemas diferentes com nomes
parecidos, e confundi-los e o que faz o defeito parecer resolvido.

O PR#2 do William, portado nesta mesma noite, deixa a ingestao **mais facil de
disparar**, com fontes declaradas e disparo por nome. Facilitar o disparo
aumenta a chance de alguem disparar duas. Por isso a corrida foi consertada
junto e nao depois.

## Por que um modulo proprio

Pelo criterio de lego desta casa: nao importa nada de `delegation_core`, recebe
o caminho por argumento e serve a qualquer arquivo. Da para arrancar, testar e
trocar sem tocar em vizinho nenhum. Se um dia entrar uma biblioteca de trava de
verdade, troca-se este arquivo e mais nada.

## Portabilidade

POSIX usa `fcntl.flock`, Windows usa `msvcrt.locking`. A diferenca importa
porque o `delegation-core` tem instalacao Windows em campo, e uma trava que so
funciona no Linux daria a impressao de proteger quem nao esta protegido. Onde
nenhum dos dois existir, o bloco roda **sem trava e avisa**, porque falhar a
ingestao por falta de trava e pior que a corrida que a trava evita.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger("locking")

try:                                  # POSIX
    import fcntl
    _POSIX = True
except ImportError:                   # pragma: no cover - so no Windows
    fcntl = None                      # type: ignore[assignment]
    _POSIX = False

try:                                  # Windows
    import msvcrt
    _WINDOWS = True
except ImportError:                   # pragma: no cover - so em POSIX
    msvcrt = None                     # type: ignore[assignment]
    _WINDOWS = False


#: Quanto esperar pela trava antes de desistir. Trinta segundos cobre uma
#: ingestao normal terminando de gravar; alem disso e sinal de processo travado,
#: e esperar mais so transfere o problema para quem chamou.
ESPERA_PADRAO = 30.0

#: De quanto em quanto tempo tentar de novo. Curto o bastante para nao somar
#: latencia perceptivel, longo o bastante para nao girar a CPU.
INTERVALO = 0.05


class TravaIndisponivel(TimeoutError):
    """A trava nao foi obtida dentro do prazo.

    Tipo proprio para quem chama poder distinguir "nao consegui a trava" de
    "o arquivo nao abriu", que pedem acoes diferentes: a primeira e esperar ou
    desistir da operacao, a segunda e um erro de ambiente.
    """


def _tentar_travar(fh) -> bool:
    """Uma tentativa nao bloqueante. Devolve se conseguiu."""
    try:
        if _POSIX:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        if _WINDOWS:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
    except OSError:
        return False
    return False


def _destravar(fh) -> None:
    try:
        if _POSIX:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        elif _WINDOWS:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


@contextmanager
def arquivo_travado(caminho: Path | str, espera: float = ESPERA_PADRAO):
    """Segura uma trava exclusiva enquanto o bloco roda.

    A trava e pega num arquivo `.lock` ao lado do alvo, e nao no alvo. Travar o
    proprio arquivo obrigaria a mante-lo aberto durante todo o ler-modificar-
    escrever, e o `os.replace` da escrita atomica troca o inode debaixo da
    trava, que a solta sem ninguem perceber.

    Levanta `TravaIndisponivel` se nao conseguir dentro de `espera`. Roda sem
    trava, com aviso, quando a plataforma nao oferece nenhum dos dois
    mecanismos.
    """
    alvo = Path(caminho)
    trava = alvo.with_suffix(alvo.suffix + ".lock")
    trava.parent.mkdir(parents=True, exist_ok=True)

    if not (_POSIX or _WINDOWS):      # pragma: no cover - plataforma exotica
        logger.warning("sem mecanismo de trava nesta plataforma; %s roda sem "
                       "protecao contra escrita concorrente", alvo.name)
        yield
        return

    # Binario de proposito: o arquivo de trava nunca tem conteudo lido nem
    # escrito, so o descritor importa. Abrir em texto obrigaria a declarar um
    # encoding que nada usa, e a regra da casa sobre encoding explicito existe
    # para arquivo que carrega texto de verdade.
    fh = open(trava, "a+b")
    limite = time.monotonic() + espera
    try:
        while True:
            if _tentar_travar(fh):
                break
            if time.monotonic() >= limite:
                raise TravaIndisponivel(
                    f"trava de {alvo.name} ocupada por mais de {espera:.0f}s")
            time.sleep(INTERVALO)
        try:
            yield
        finally:
            _destravar(fh)
    finally:
        fh.close()


def ler_modificar_escrever(caminho: Path | str, ler, escrever, modificar,
                           espera: float = ESPERA_PADRAO):
    """O ciclo inteiro sob uma trava so, que e o ponto deste modulo.

    Separar `ler` e `escrever` e a diferenca entre proteger e parecer proteger:
    um `ler` travado seguido de um `escrever` travado deixa exatamente a janela
    que fez as entradas sumirem em 31/08.

    `modificar` recebe o que `ler` devolveu e devolve o que `escrever` grava.
    """
    with arquivo_travado(caminho, espera):
        dados = ler()
        novo = modificar(dados)
        escrever(novo)
        return novo
