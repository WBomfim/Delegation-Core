"""Hooks de sessao como parte do pacote: um comando, um processo por evento.

Ate a v0.14.0 eram tres scripts copiados para ~/.delegation_core/hooks/ e
registrados a mao. Em 03/10/2026 a copia do session_export.py estava atras da
arvore, e so o doctor percebeu. O fim de sessao abria dois processos. Agora o
registro aponta para `delegation-core-hook` e o instalador e quem o escreve.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from delegation_core import clients
from delegation_core.hooks import entrada


# ── o comando ────────────────────────────────────────────────────────────────


def test_fim_de_sessao_roda_os_dois_passos_com_a_mesma_entrada(monkeypatch):
    from delegation_core.hooks import llama_session_stop, session_export

    vistos = []
    monkeypatch.setattr(session_export, "main", lambda raw: vistos.append(("export", raw)))
    monkeypatch.setattr(llama_session_stop, "main", lambda raw: vistos.append(("stop", raw)))
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id": "abc"}'))
    assert entrada.main(["session-end"]) == 0
    assert vistos == [("export", '{"session_id": "abc"}'), ("stop", '{"session_id": "abc"}')]


def test_um_passo_que_falha_nao_impede_o_outro(monkeypatch, capsys):
    from delegation_core.hooks import llama_session_stop, session_export

    parou = []

    def explode(raw):
        raise RuntimeError("disco cheio")

    monkeypatch.setattr(session_export, "main", explode)
    monkeypatch.setattr(llama_session_stop, "main", lambda raw: parou.append(raw))
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert entrada.main(["session-end"]) == 0
    assert parou == ["{}"]
    assert "session_export falhou" in capsys.readouterr().err


def test_o_export_nao_encerra_o_processo(monkeypatch):
    """session_export.main usava sys.exit(0); no processo unico isso mataria
    o passo seguinte."""
    from delegation_core.hooks import session_export

    assert session_export.main("") == 0
    assert session_export.main("nao e json") == 0


def test_evento_desconhecido_e_erro_de_uso():
    assert entrada.main(["session-middle"]) == 2


def test_o_comando_esta_declarado_no_pyproject():
    texto = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert 'delegation-core-hook = "delegation_core.hooks.entrada:main"' in texto


def test_nao_ha_mais_pasta_hooks_solta_no_repositorio():
    assert not (Path(__file__).resolve().parents[1] / "hooks").exists()


# ── o registro ───────────────────────────────────────────────────────────────


def test_a_suite_nao_enxerga_o_settings_real():
    assert clients.CLAUDE_SETTINGS != Path.home() / ".claude" / "settings.json"


def _settings(tmp_path, dados) -> Path:
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(dados), encoding="utf-8")
    return p


def _comandos(path: Path, evento: str) -> list[str]:
    dados = json.loads(path.read_text(encoding="utf-8"))
    return [h["command"] for g in dados["hooks"].get(evento, []) for h in g["hooks"]]


def test_registro_troca_os_scripts_copiados_e_preserva_os_de_terceiros(tmp_path):
    p = _settings(tmp_path, {"hooks": {
        "SessionStart": [{"matcher": "*", "hooks": [
            {"type": "command", "command": "python3 /home/u/.delegation_core/hooks/session_start_brief.py"},
            {"type": "command", "command": "outro-programa --x"}]}],
        "SessionEnd": [{"matcher": "*", "hooks": [
            {"type": "command", "command": "python3 /home/u/.delegation_core/hooks/session_export.py"}]},
            {"matcher": "*", "hooks": [
            {"type": "command", "command": "python3 /home/u/.delegation_core/hooks/llama_session_stop.py"}]}],
    }, "model": "x"})
    exe = tmp_path / "bin" / "delegation-core-hook"
    r = clients.register_session_hooks(settings_path=p, executable=exe)
    assert r["status"] == "updated" and r["replaced"] == 3
    assert _comandos(p, "SessionStart") == ["outro-programa --x", f'"{exe}" session-start']
    assert _comandos(p, "SessionEnd") == [f'"{exe}" session-end']
    assert json.loads(p.read_text())["model"] == "x"
    assert (tmp_path / "settings.json.dc-backup").exists()


def test_registro_e_idempotente(tmp_path):
    p = _settings(tmp_path, {})
    exe = tmp_path / "delegation-core-hook"
    assert clients.register_session_hooks(settings_path=p, executable=exe)["status"] == "installed"
    assert clients.register_session_hooks(settings_path=p, executable=exe)["status"] == "already-configured"
    assert len(_comandos(p, "SessionEnd")) == 1


def test_json_invalido_nao_e_tocado(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{ quebrado", encoding="utf-8")
    assert clients.register_session_hooks(settings_path=p)["status"] == "error"
    assert p.read_text() == "{ quebrado"


def test_registro_apaga_as_copias_antigas(tmp_path):
    from delegation_core import config
    pasta = Path(config.CONFIG_DIR) / "hooks"
    pasta.mkdir(parents=True, exist_ok=True)
    for nome in clients.LEGACY_HOOK_SCRIPTS:
        (pasta / nome).write_text("# copia antiga")
    r = clients.register_session_hooks(settings_path=_settings(tmp_path, {}),
                                       executable=tmp_path / "x")
    assert sorted(r["legacy_copies_removed"]) == sorted(clients.LEGACY_HOOK_SCRIPTS)
    assert not pasta.exists()


def test_desregistro_tira_so_os_nossos(tmp_path):
    p = _settings(tmp_path, {"hooks": {"SessionEnd": [{"matcher": "*", "hooks": [
        {"type": "command", "command": '"/v/bin/delegation-core-hook" session-end'},
        {"type": "command", "command": "outro"}]}]}})
    r = clients.unregister_session_hooks(settings_path=p)
    assert r["status"] == "removed" and r["removed"] == 1
    assert _comandos(p, "SessionEnd") == ["outro"]


# ── o doctor ─────────────────────────────────────────────────────────────────


def test_doctor_ok_com_o_registro_novo(tmp_path):
    from delegation_core import doctor
    exe = tmp_path / "delegation-core-hook"
    exe.write_text("")
    p = _settings(tmp_path, {})
    clients.register_session_hooks(settings_path=p, executable=exe)
    assert doctor.check_hooks(p)["status"] == "ok"


def test_doctor_avisa_registro_antigo_e_executavel_sumido(tmp_path):
    from delegation_core import doctor
    antigo = _settings(tmp_path, {"hooks": {"SessionEnd": [{"hooks": [
        {"type": "command", "command": "python3 /h/.delegation_core/hooks/session_export.py"}]}]}})
    r = doctor.check_hooks(antigo)
    assert r["status"] == "warn" and "session_export.py" in r["detail"]
    sumido = tmp_path / "s2.json"
    sumido.write_text("{}")
    clients.register_session_hooks(settings_path=sumido,
                                   executable=tmp_path / "venv-apagado" / "delegation-core-hook")
    r = doctor.check_hooks(sumido)
    assert r["status"] == "warn" and "executable missing" in r["detail"]


def test_doctor_avisa_quando_nada_esta_registrado(tmp_path):
    from delegation_core import doctor
    r = doctor.check_hooks(_settings(tmp_path, {}))
    assert r["status"] == "warn" and "not registered" in r["detail"]
