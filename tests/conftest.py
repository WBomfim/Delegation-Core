"""Guarda de suite: nenhum teste escreve em ~/.delegation_core.

Isto existe por causa de um estrago real, nao de uma preocupacao teorica.

Em 02/09/2026 um teste novo chamou `DelegationEngine.calibrate()`. O ultimo
que calibrate faz e `self.cfg.save()`, e `Config.save()` escreve em
`config.CONFIG_FILE`, que e uma constante de modulo apontando para
`~/.delegation_core/config.json`. O objeto Config nao sabe de onde veio: um
Config construido pelo teste com `vault_path=tmp_path` grava por cima da
config real da maquina exatamente como o Config carregado dela gravaria.

O resultado: o `config.json` do usuario passou a apontar para
`/tmp/pytest-of-joey/pytest-76/...`, o daemon subiu, nao encontrou vault,
imprimiu "delegation-core is not configured", e o systemd desistiu depois de
cinco tentativas. A maquina ficou sem servico ate a config ser reescrita a mao.

Ja tinha acontecido antes. O HANDOFF registra a correcao de 28/07: o fixture de
`test_dashboard_api_routes.py` passou a apontar CONFIG_DIR/CONFIG_FILE para
tmp_path. A correcao foi feita em UM arquivo, e a suite tem 65. Uma defesa que
depende de cada autor de teste lembrar dela nao e uma defesa.

Por isso o redirecionamento aqui e `autouse=True`: vale para todo teste da
suite, escrito antes ou depois, sem que ninguem precise pedir. Um teste que
QUEIRA exercitar o caminho de escrita continua podendo, e escreve no tmp_path
que este fixture instalou.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


#: Modulos que carregam CONFIG_DIR/CONFIG_FILE de `config` por `from ... import`
#: e portanto guardam a propria referencia, que reapontar `config.CONFIG_DIR`
#: sozinho nao alcanca.
_MODULOS_COM_COPIA = (
    "delegation_core.cli",
    "delegation_core.dashboard_api",
    "delegation_core.wizard",
    "delegation_core.doctor",
    "delegation_core.service",
    "delegation_core.clients",
    "delegation_core.windows",
    "delegation_core.localqueue",
    "delegation_core.tracker",
    "delegation_core.downloader",
    # installer.uninstall() REMOVE arquivos sob CONFIG_DIR. Um teste que o
    # chamasse sem este redirecionamento apagaria hooks/, sessions/, graphs/ e
    # a config da maquina de verdade. E a mesma copia de `from .config import
    # CONFIG_DIR` dos outros, so que destrutiva.
    "delegation_core.installer",
)


#: O estado real, calculado antes de qualquer teste mexer em HOME ou em modulo.
_ESTADO_REAL = (Path.home() / ".delegation_core").absolute()


def _dentro_do_estado_real(caminho: Path) -> bool:
    try:
        return Path(caminho).absolute().is_relative_to(_ESTADO_REAL)
    except (OSError, ValueError, RuntimeError):
        return False


_PACOTE_IMPORTADO = False


def _importar_o_pacote_inteiro():
    """Importa todo submodulo, para que a varredura abaixo alcance os
    caminhos de modulo que um teste so importaria depois do fixture. Uma vez
    por sessao: depois disso os modulos ja estao em sys.modules."""
    global _PACOTE_IMPORTADO
    if _PACOTE_IMPORTADO:
        return
    _PACOTE_IMPORTADO = True
    import pkgutil

    import delegation_core
    for info in pkgutil.walk_packages(delegation_core.__path__, "delegation_core."):
        if info.name.endswith("__main__"):
            continue          # importar __main__ executa a CLI com o argv do pytest
        try:
            importlib.import_module(info.name)
        except Exception:
            continue          # extra opcional ausente nao pode quebrar a suite


@pytest.fixture(autouse=True)
def _sem_escrita_no_estado_real(tmp_path, monkeypatch):
    """Reaponta todo caminho de estado do usuario para um diretorio temporario."""
    import importlib

    from delegation_core import config as config_mod

    raiz = tmp_path / "estado_delegation_core"
    raiz.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(config_mod, "CONFIG_DIR", raiz, raising=False)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", raiz / "config.json", raising=False)

    # `from .config import CONFIG_DIR` copia o valor no import. Reapontar so o
    # modulo de origem deixa essas copias apontando para a casa do usuario.
    for nome in _MODULOS_COM_COPIA:
        try:
            mod = importlib.import_module(nome)
        except Exception:
            continue          # extra opcional ausente nao pode quebrar a suite
        for atributo, valor in (("CONFIG_DIR", raiz),
                                ("CONFIG_FILE", raiz / "config.json"),
                                ("STORE_PATH", raiz / "local_tasks.json")):
            if hasattr(mod, atributo):
                monkeypatch.setattr(mod, atributo, valor, raising=False)

    # Qualquer OUTRO caminho de modulo que caia dentro do estado real, com
    # qualquer nome. A lista acima cobre CONFIG_DIR, CONFIG_FILE e STORE_PATH,
    # e em 29/09/2026 isso nao bastou: `ingest._REGISTRY_FILE` e calculado no
    # import, nenhum dos tres nomes o alcanca, e um teste novo que chamou
    # `_save_registry` sem reapontar o nome a mao gravou uma fonte do pytest
    # por cima do `ingested_sources.json` real. Aconteceu em duas maquinas:
    # aqui o registro de 23 fontes virou 1, e num Mac o de 172 virou 1, o que
    # fez a reconstrucao do indice de la pular todas as fontes externas. Oito
    # testes ja faziam o reapontamento a mao; o nono esqueceu.
    _importar_o_pacote_inteiro()
    for nome, mod in list(sys.modules.items()):
        if not nome.startswith("delegation_core") or mod is None:
            continue
        for atributo, valor in list(vars(mod).items()):
            if isinstance(valor, Path) and _dentro_do_estado_real(valor):
                monkeypatch.setattr(mod, atributo,
                                    raiz / valor.relative_to(_ESTADO_REAL), raising=False)

    # ~/.claude/settings.json fica fora de ~/.delegation_core, e desde a v0.15.0
    # o instalador registra os hooks de sessao nele. Os testes do instalador
    # chamam post_install, update e uninstall: sem isto, gravariam no arquivo
    # real de quem roda a suite.
    from delegation_core import clients as clients_mod
    monkeypatch.setattr(clients_mod, "CLAUDE_SETTINGS", raiz / "claude_settings.json",
                        raising=False)

    yield raiz



@pytest.fixture(autouse=True)
def _sem_gerenciador_de_servico_real(tmp_path, monkeypatch):
    """Nenhum teste executa systemctl/launchctl/schtasks de verdade.

    ESCRITO DEPOIS DO ESTRAGO, em 03/09/2026. Um teste novo chamou
    `installer.uninstall(dry_run=False)`. O redirecionamento de CONFIG_DIR
    acima protegeu os ARQUIVOS: nada foi apagado de ~/.delegation_core. Mas
    `uninstall` tambem desregistra os servicos, e essa metade nao passa por
    CONFIG_DIR nenhum: passa por `service._run(["systemctl", "--user",
    "disable", "--now", "delegation-core"])` com o nome REAL da unit.

    Resultado medido: o daemon da maquina ficou `inactive`, a unit
    `~/.config/systemd/user/delegation-core.service` foi removida, e
    `systemctl --user is-enabled` passou a responder `not-found`. O teste
    ainda levou 16,6s no lugar de 0,2s, porque estava esperando o systemd de
    verdade. Foi preciso `delegation-core service install` para reverter.

    A licao e a que este arquivo ja abre dizendo sobre o `calibrate()`: o
    perigo nao e o teste que se sabe destrutivo, e a funcao que faz mais do que
    o nome do fixture cobre. Por isso isto e `autouse` e nao um opt-in.

    Devolve (127, ...), que e a forma de "comando indisponivel" que os
    chamadores ja tratam: uma maquina sem systemd responde a mesma coisa.
    """
    from delegation_core import service as service_mod

    def _bloqueado(cmd, timeout=30):
        return (127, "blocked by the test suite: no test runs a real service manager")

    monkeypatch.setattr(service_mod, "_run", _bloqueado, raising=False)

    # A sonda de porta tambem sai, e por um motivo diferente do bloqueio acima:
    # sem ela o resultado do teste depende de o daemon do usuario estar de pe
    # neste instante. `installer.uninstall` recusa com "refused_daemon_still_up"
    # quando algo responde na porta, entao o mesmo teste passava na maquina de
    # quem tinha o servico parado e falhava na de quem nao tinha. Falso na
    # suite significa "nao ha daemon aqui", que e o mundo em que um teste roda.
    monkeypatch.setattr(service_mod, "_port_answers", lambda: False, raising=False)

    # E os CAMINHOS das units, que e por onde o estrago passou de verdade.
    # Bloquear `_run` tira os `systemctl disable`, mas `service.uninstall` apaga
    # o arquivo com `SYSTEMD_UNIT.unlink(missing_ok=True)`, direto, sem passar
    # por comando nenhum. Foi assim que a unit real caiu uma SEGUNDA vez, ja com
    # o bloqueio de `_run` no lugar: a rede de seguranca abaixo pegou e nomeou o
    # teste culpado, que e exatamente para isso que ela existe.
    unidades = tmp_path / "systemd-de-mentira"
    unidades.mkdir(parents=True, exist_ok=True)
    for atributo, destino in (
        ("SYSTEMD_UNIT", unidades / "delegation-core.service"),
        ("LLAMA_SYSTEMD_UNIT", unidades / "delegation-core-llama.service"),
        ("LAUNCHD_PLIST", unidades / "com.delegation-core.plist"),
        ("LLAMA_LAUNCHD_PLIST", unidades / "com.delegation-core.llama.plist"),
    ):
        monkeypatch.setattr(service_mod, atributo, destino, raising=False)


@pytest.fixture(autouse=True)
def _units_do_systemd_intactas():
    """Rede de seguranca para o mesmo estrago, caso alguem contorne o bloqueio.

    Falha o teste culpado em vez de deixar a maquina sem daemon para quem for
    rodar a suite depois. Mesma forma da rede de hooks/venv/models acima, que
    nao cobria isto porque as units vivem fora de ~/.delegation_core.
    """
    unidades = Path.home() / ".config" / "systemd" / "user"
    antes = {p.name for p in unidades.glob("delegation-core*.service")} if unidades.exists() else set()

    yield

    depois = {p.name for p in unidades.glob("delegation-core*.service")} if unidades.exists() else set()
    sumiram = antes - depois
    if sumiram:
        pytest.fail(
            f"o teste removeu {', '.join(sorted(sumiram))} de {unidades}. "
            "Nenhum teste desregistra servico real: use um dublê de service._run. "
            "Para reverter a mao: delegation-core service install"
        )

@pytest.fixture(autouse=True)
def _config_real_intacta():
    """Rede de seguranca: falha o teste que ainda assim tocar a config real.

    O fixture acima cobre os caminhos conhecidos. Este cobre os que ninguem
    mapeou ainda, e falha o teste culpado em vez de deixar a maquina quebrada
    para quem for rodar a suite depois.
    """
    real = Path.home() / ".delegation_core" / "config.json"
    antes = real.read_bytes() if real.exists() else None

    # Desde que `installer.uninstall()` existe, a suite tem uma funcao que
    # APAGA diretorios sob ~/.delegation_core. Conferir so o config.json
    # deixaria passar um teste que removesse hooks/ ou o venv, que e um
    # estrago maior e mais silencioso do que o que criou este arquivo.
    raiz_real = Path.home() / ".delegation_core"
    vigiados = ("hooks", "venv", "models")
    presentes_antes = {n for n in vigiados if (raiz_real / n).exists()}

    yield

    sumiram = presentes_antes - {n for n in vigiados if (raiz_real / n).exists()}
    if sumiram:
        pytest.fail(
            f"o teste apagou {', '.join(sorted(sumiram))} de {raiz_real}. "
            "Nenhum teste pode remover estado real: use o tmp_path que "
            "conftest._sem_escrita_no_estado_real ja instalou. Isto nao pode "
            "ser desfeito automaticamente."
        )

    depois = real.read_bytes() if real.exists() else None
    if antes != depois:
        # Devolve o arquivo antes de falhar: o teste ja errou, a maquina do
        # usuario nao precisa pagar por isso.
        if antes is None:
            real.unlink(missing_ok=True)
        else:
            real.write_bytes(antes)
        pytest.fail(
            f"o teste escreveu em {real}. Nenhum teste pode tocar o estado real "
            "da maquina: use o tmp_path que conftest._sem_escrita_no_estado_real "
            "ja instalou. O arquivo foi restaurado."
        )
