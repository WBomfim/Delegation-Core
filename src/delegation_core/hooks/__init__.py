"""Os hooks de sessao do Claude Code, como parte do pacote.

Ate a v0.14.0 eram tres scripts soltos em `hooks/`, copiados para
`~/.delegation_core/hooks/` e registrados a mao no `settings.json`. A copia
envelhecia sem aviso (o `doctor` ganhou um check so para isso) e o fim de
sessao abria dois processos. Agora rodam de dentro do pacote instalado, pelo
comando `delegation-core-hook`, com um processo por evento. Ver `entrada.py`.

Os modulos daqui so usam a biblioteca padrao: rodam a cada inicio e fim de
sessao, e nao podem pagar o import do resto do pacote.
"""
