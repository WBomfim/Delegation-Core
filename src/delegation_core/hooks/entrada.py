"""`delegation-core-hook`: um processo por evento de sessao do Claude Code.

    delegation-core-hook session-start   resumo do que mudou no vault
    delegation-core-hook session-end     exporta a transcricao e para o
                                         modelo local ocioso

O fim de sessao le a entrada do hook uma vez e entrega aos dois passos, em
sequencia, no mesmo processo. Antes eram dois hooks registrados e dois
interpretadores. Um passo que falha nao impede o outro, e nada aqui devolve
erro ao Claude Code: o encerramento da sessao nunca pode quebrar por causa de
um hook.
"""

from __future__ import annotations

import sys
from typing import Callable

USO = "uso: delegation-core-hook {session-start|session-end}"


def _passo(nome: str, fazer: Callable[[], object]) -> None:
    try:
        fazer()
    except Exception as e:  # um passo nunca derruba o outro nem a sessao
        sys.stderr.write(f"delegation-core-hook: {nome} falhou: {type(e).__name__}: {e}\n")


def session_start() -> None:
    from . import session_start_brief
    _passo("session_start_brief", session_start_brief.main)


def session_end(raw: str) -> None:
    from . import llama_session_stop, session_export
    _passo("session_export", lambda: session_export.main(raw))
    _passo("llama_session_stop", lambda: llama_session_stop.main(raw))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    evento = args[0] if args else ""
    if evento == "session-start":
        session_start()
        return 0
    if evento == "session-end":
        try:
            raw = sys.stdin.read()
        except Exception:
            raw = ""
        session_end(raw)
        return 0
    sys.stderr.write(USO + "\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
