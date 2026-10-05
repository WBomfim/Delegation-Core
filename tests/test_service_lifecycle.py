"""Parar e religar o daemon, que e o que faltava para um upgrade seguro.

`service.py` sabia instalar, desinstalar e consultar, e nao sabia PARAR. O
instalador, que e o unico caminho de update que este projeto tem, roda
`pip install` dentro do venv de onde o daemon esta rodando e nunca o para
antes. No Linux isso passa e o daemon segue com o codigo velho ate alguem
reinicia-lo a mao; no Windows o pip nao consegue substituir arquivo que um
processo mantem aberto, e o erro era engolido por `>nul 2>&1`.

Tudo aqui usa fakes. A regra do projeto e explicita: nenhum teste toca o
gerenciador de servico real, o vault real ou carrega modelo. O comportamento
contra o daemon de verdade foi medido a mao antes destes testes existirem:

    stop()     stopped   em 516 ms
    start()    started   em   4 ms
    is_up()                False logo apos start, True 1,0 s depois
    restart()  restarted em 223 ms

Esse ultimo par e o motivo de `is_up` existir: `start` devolve quando o
gerenciador ACEITA o pedido, nao quando o daemon atende.
"""
from __future__ import annotations

import pytest

from delegation_core import service


@pytest.fixture
def comandos(monkeypatch):
    """Captura o que teria sido executado, e deixa o teste ditar o resultado."""
    registro = {"chamadas": [], "resultado": (0, "")}

    def _fake_run(cmd, timeout=30):
        registro["chamadas"].append({"cmd": cmd, "timeout": timeout})
        return registro["resultado"]

    monkeypatch.setattr(service, "_run", _fake_run)
    return registro


def _em(sistema, monkeypatch):
    monkeypatch.setattr(service.platform, "system", lambda: sistema)


def _startup_isolado(monkeypatch, tmp_path):
    """Prevent Windows fallback tests from observing the real Startup folder."""
    monkeypatch.setattr(service, "WIN_STARTUP_DIR", tmp_path)
    monkeypatch.setattr(service, "WIN_STARTUP_CMD", tmp_path / "delegation-core.cmd")
    monkeypatch.setattr(service, "WIN_STARTUP_VBS", tmp_path / "delegation-core.vbs")


# ── o timeout, que e a razao de stop existir separado ───────────────────────

def test_o_teto_de_parada_acompanha_a_unit():
    """A unit declara TimeoutStopSec=600. Um stop que desiste antes disso
    reporta falha enquanto o systemd ainda esta encerrando corretamente."""
    assert service.STOP_TIMEOUT_SEC == 600
    assert "TimeoutStopSec=600" in service.systemd_unit_text()


def test_stop_usa_o_teto_longo_e_nao_o_padrao_de_30s(comandos, monkeypatch):
    _em("Linux", monkeypatch)
    service.stop()
    assert comandos["chamadas"][0]["timeout"] == service.STOP_TIMEOUT_SEC


def test_start_nao_precisa_do_teto_longo(comandos, monkeypatch):
    """Subir e imediato; so a parada pode levar minutos."""
    _em("Linux", monkeypatch)
    service.start()
    assert comandos["chamadas"][0]["timeout"] == 30


def test_o_teto_pode_ser_encurtado_por_quem_chama(comandos, monkeypatch):
    _em("Linux", monkeypatch)
    service.stop(timeout=5)
    assert comandos["chamadas"][0]["timeout"] == 5


# ── por plataforma ──────────────────────────────────────────────────────────

def test_linux_para_e_sobe_pelo_systemctl(comandos, monkeypatch):
    _em("Linux", monkeypatch)
    assert service.stop()["status"] == "stopped"
    assert service.start()["status"] == "started"
    assert comandos["chamadas"][0]["cmd"] == ["systemctl", "--user", "stop", "delegation-core"]
    assert comandos["chamadas"][1]["cmd"] == ["systemctl", "--user", "start", "delegation-core"]


def test_macos_para_SEM_o_dash_w(comandos, monkeypatch, tmp_path):
    """`-w` e o que marca o agente habilitado entre logins.

    `install()` usa `-w` de proposito. Passa-lo aqui desabilitaria o agente, e
    quem so queria pausar o daemon para um upgrade o encontraria sumido depois
    do proximo reboot.
    """
    _em("Darwin", monkeypatch)
    plist = tmp_path / "com.delegation-core.plist"
    plist.write_text("<plist/>", encoding="utf-8")
    monkeypatch.setattr(service, "LAUNCHD_PLIST", plist)

    service.stop()
    cmd = comandos["chamadas"][0]["cmd"]
    assert cmd[:2] == ["launchctl", "unload"]
    assert "-w" not in cmd, "stop desabilitaria o agente entre logins"


def test_macos_sem_plist_diz_que_nao_esta_instalado(comandos, monkeypatch, tmp_path):
    _em("Darwin", monkeypatch)
    monkeypatch.setattr(service, "LAUNCHD_PLIST", tmp_path / "nao_existe.plist")
    assert service.stop()["status"] == "not_installed"
    assert service.start()["status"] == "not_installed"
    assert comandos["chamadas"] == [], "chamou launchctl para um plist inexistente"


def test_windows_encerra_e_roda_a_tarefa(comandos, monkeypatch):
    _em("Windows", monkeypatch)
    service.stop(); service.start()
    assert comandos["chamadas"][0]["cmd"] == ["schtasks", "/End", "/TN", "delegation-core"]
    assert comandos["chamadas"][1]["cmd"] == ["schtasks", "/Run", "/TN", "delegation-core"]


class _Proc:
    def __init__(self, pid, cmdline):
        self.pid, self._cmd, self.terminado = pid, cmdline, False

    def cmdline(self):
        return self._cmd

    def terminate(self):
        self.terminado = True


def test_windows_no_modo_atalho_stop_encerra_o_processo_do_daemon(comandos, monkeypatch, tmp_path):
    """Sem tarefa agendada (instalacao sem elevacao) o stop respondia
    "not_installed" e o processo seguia de pe. O update entao nao conseguia
    religar nem substituir os arquivos, e sobrava subir a mao: num Windows de
    02/10/2026 isso deixou o daemon rodando pelo Python global do PATH."""
    _em("Windows", monkeypatch)
    _startup_isolado(monkeypatch, tmp_path)
    comandos["resultado"] = (1, "ERROR: The system cannot find the file specified.")
    service.WIN_STARTUP_CMD.write_text("@echo off", encoding="utf-8")
    daemon = _Proc(10, [r"C:\Python312\python.exe", "-m", "delegation_core", "run"])
    outro = _Proc(11, [r"C:\Python312\python.exe", "-m", "http.server"])
    monkeypatch.setattr(service, "_processos_candidatos", lambda: [daemon, outro])
    monkeypatch.setattr(service, "_esperar_fim", lambda procs, timeout: [])

    r = service.stop()
    assert r["status"] == "stopped"
    assert daemon.terminado and not outro.terminado


def test_windows_no_modo_atalho_sem_processo_rodando_e_not_running(comandos, monkeypatch, tmp_path):
    _em("Windows", monkeypatch)
    _startup_isolado(monkeypatch, tmp_path)
    comandos["resultado"] = (1, "ERROR")
    service.WIN_STARTUP_CMD.write_text("@echo off", encoding="utf-8")
    monkeypatch.setattr(service, "_processos_candidatos", lambda: [])
    assert service.stop()["status"] == "not_running"


def test_windows_no_modo_atalho_start_executa_o_launcher(comandos, monkeypatch, tmp_path):
    """O launcher e o que o instalador gravou com o Python do venv; subir por
    ele e o que garante o interpretador certo."""
    _em("Windows", monkeypatch)
    _startup_isolado(monkeypatch, tmp_path)
    comandos["resultado"] = (1, "ERROR")
    service.WIN_STARTUP_CMD.write_text("@echo off", encoding="utf-8")
    abertos = []
    monkeypatch.setattr(service, "_abrir_launcher", lambda caminho: abertos.append(caminho))
    assert service.start()["status"] == "started"
    assert abertos == [service.WIN_STARTUP_CMD]


def test_windows_sem_tarefa_e_sem_fallback_e_falha(comandos, monkeypatch, tmp_path):
    _em("Windows", monkeypatch)
    _startup_isolado(monkeypatch, tmp_path)
    comandos["resultado"] = (1, "ERROR")
    assert service.stop()["status"] == "failed"


def test_windows_fallback_cria_cmd_visivel_com_python_e_remove_vbs_legado(comandos, monkeypatch, tmp_path):
    _em("Windows", monkeypatch)
    _startup_isolado(monkeypatch, tmp_path)
    comandos["resultado"] = (1, "ERROR: Access denied")
    service.WIN_STARTUP_VBS.write_text("launcher", encoding="utf-8")

    result = service.install()

    assert result["status"] == "installed"
    assert result["unit"] == str(service.WIN_STARTUP_CMD)
    assert not service.WIN_STARTUP_VBS.exists()
    launcher = service.WIN_STARTUP_CMD.read_text(encoding="utf-8")
    assert launcher.startswith("@echo off")
    assert f'"{service.sys.executable}" -m delegation_core run' in launcher


def test_windows_task_uses_python_and_removes_stale_startup_launchers(comandos, monkeypatch, tmp_path):
    _em("Windows", monkeypatch)
    _startup_isolado(monkeypatch, tmp_path)
    service.WIN_STARTUP_CMD.write_text("legacy", encoding="utf-8")
    service.WIN_STARTUP_VBS.write_text("legacy", encoding="utf-8")

    result = service.install()

    assert result["unit"] == "Task Scheduler: delegation-core"
    assert not service.WIN_STARTUP_CMD.exists()
    assert not service.WIN_STARTUP_VBS.exists()
    assert comandos["chamadas"][0]["cmd"][7] == service._windows_run_command()


def test_windows_uninstall_remove_os_dois_launchers(comandos, monkeypatch, tmp_path):
    _em("Windows", monkeypatch)
    _startup_isolado(monkeypatch, tmp_path)
    comandos["resultado"] = (1, "ERROR")
    service.WIN_STARTUP_CMD.write_text("@echo off", encoding="utf-8")
    service.WIN_STARTUP_VBS.write_text("launcher", encoding="utf-8")

    result = service.uninstall()

    assert result["status"] == "removed"
    assert not service.WIN_STARTUP_CMD.exists()
    assert not service.WIN_STARTUP_VBS.exists()


def test_plataforma_desconhecida_nao_explode(comandos, monkeypatch):
    _em("Haiku", monkeypatch)
    assert service.stop()["status"] == "unsupported"
    assert service.start()["status"] == "unsupported"


# ── falha propagada ─────────────────────────────────────────────────────────

def test_stop_que_falha_e_reportado_como_falha(comandos, monkeypatch):
    _em("Linux", monkeypatch)
    comandos["resultado"] = (1, "Failed to stop delegation-core.service")
    r = service.stop()
    assert r["status"] == "failed"
    assert "Failed to stop" in r["detail"]


def test_stop_que_estoura_o_prazo_nao_vira_sucesso(comandos, monkeypatch):
    """124 e o codigo que _run devolve num TimeoutExpired."""
    _em("Linux", monkeypatch)
    comandos["resultado"] = (124, "systemctl timed out after 600s")
    assert service.stop()["status"] == "failed"


# ── restart ─────────────────────────────────────────────────────────────────

def test_restart_para_e_sobe_nessa_ordem(comandos, monkeypatch):
    _em("Linux", monkeypatch)
    r = service.restart()
    assert r["status"] == "restarted"
    assert [c["cmd"][2] for c in comandos["chamadas"]] == ["stop", "start"]


def test_restart_nao_tenta_subir_se_a_parada_falhou(comandos, monkeypatch):
    """Subir por cima de um daemon que nao morreu deixa dois processos no
    mesmo indice, que e pior que nao ter reiniciado."""
    _em("Linux", monkeypatch)
    comandos["resultado"] = (1, "Failed to stop")
    r = service.restart()

    assert r["status"] == "failed"
    assert r["start"] is None
    assert len(comandos["chamadas"]) == 1, "chamou start apos um stop que falhou"


def test_restart_reporta_as_duas_metades(comandos, monkeypatch):
    _em("Linux", monkeypatch)
    r = service.restart()
    assert r["stop"]["action"] == "stop"
    assert r["start"]["action"] == "start"


# ── is_up: a diferenca entre aceito e pronto ────────────────────────────────

def test_is_up_reflete_a_porta(monkeypatch):
    monkeypatch.setattr(service, "_port_answers", lambda: True)
    assert service.is_up() is True
    monkeypatch.setattr(service, "_port_answers", lambda: False)
    assert service.is_up() is False


def test_is_up_espera_ate_o_daemon_atender(monkeypatch):
    """Medido no daemon real: start devolve em 4 ms e a porta so responde 1 s
    depois, porque o processo ainda esta carregando o BGE."""
    respostas = iter([False, False, True])
    monkeypatch.setattr(service, "_port_answers", lambda: next(respostas))
    monkeypatch.setattr(service.time if hasattr(service, "time") else __import__("time"),
                        "sleep", lambda _: None)
    assert service.is_up(wait_seconds=10) is True


def test_is_up_desiste_no_prazo(monkeypatch):
    monkeypatch.setattr(service, "_port_answers", lambda: False)
    assert service.is_up(wait_seconds=0) is False


def test_is_up_sem_espera_faz_uma_unica_sondagem(monkeypatch):
    n = {"v": 0}

    def _sonda():
        n["v"] += 1
        return False

    monkeypatch.setattr(service, "_port_answers", _sonda)
    service.is_up()
    assert n["v"] == 1


# ── esperar a queda nao e esperar a subida ──────────────────────────────────


def test_esperar_a_queda_devolve_assim_que_a_porta_silencia(monkeypatch):
    """`is_up(wait_seconds=15)` respondia a pergunta oposta.

    Ele existe para o `start()`: espera ate N segundos pelo daemon SUBIR e
    devolve True assim que a porta responde. `installer.uninstall` o usava para
    perguntar se o daemon DESCEU depois de um stop, e o resultado, cronometrado
    em 03/09/2026 com a sonda de porta controlada:

        daemon ainda de pe (caso ruim)   -> True  em  0,00s
        daemon ja parou (caso bom)       -> False em 15,00s
        daemon para em 3s (o realista)   -> True  em  0,00s

    As duas metades erradas. Todo uninstall bem-sucedido pagava 15 segundos por
    nada, e uma parada graciosa de qualquer duracao era declarada "ainda de pe"
    no instante zero, com a mensagem dizendo "still answering 15s after a stop
    was requested" sem ter esperado nada. A unit deste projeto registra
    TimeoutStopSec=600 justamente porque uma passada de relink leva minutos.
    """
    from delegation_core import service

    chamadas = []

    def porta():
        chamadas.append(1)
        return len(chamadas) < 3      # responde duas vezes, depois silencia

    monkeypatch.setattr(service, "_port_answers", porta)

    assert service.wait_until_down(timeout_seconds=15, interval=0.01) is True
    assert len(chamadas) == 3, "tem que parar de sondar assim que a porta silencia"


def test_esperar_a_queda_desiste_e_diz_que_nao_caiu(monkeypatch):
    from delegation_core import service

    monkeypatch.setattr(service, "_port_answers", lambda: True)

    assert service.wait_until_down(timeout_seconds=0.05, interval=0.01) is False


def test_porta_ja_silenciosa_nao_espera_nada(monkeypatch):
    """O caso bom, que era o que pagava os 15 segundos."""
    import time

    from delegation_core import service

    monkeypatch.setattr(service, "_port_answers", lambda: False)

    t0 = time.monotonic()
    assert service.wait_until_down(timeout_seconds=15, interval=0.5) is True
    assert time.monotonic() - t0 < 1.0, "nao pode esperar quando ja esta parado"


def test_is_up_continua_esperando_a_SUBIDA(monkeypatch):
    """A funcao original nao muda: ela esta certa para o que ela e."""
    from delegation_core import service

    chamadas = []

    def porta():
        chamadas.append(1)
        return len(chamadas) >= 2

    monkeypatch.setattr(service, "_port_answers", porta)

    assert service.is_up(wait_seconds=5) is True
