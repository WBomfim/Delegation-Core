"""`delegation-core update`, que ate agora nao existia.

Atualizar significava reexecutar install.sh ou install.bat. Os dois tratam o
caso (fazem backup do pacote anterior, preservam config.json, re-registram o
servico), mas nenhum PARA o daemon antes de o pip escrever no venv de onde o
daemon esta rodando. No Linux e no macOS isso sobrevive e os scripts pedem ao
usuario que reinicie a mao; no Windows o pip nao substitui arquivo aberto por
outro processo, e a linha seguinte e `>nul 2>&1` sem checagem.

A ordem que este modulo impoe e o conteudo do teste: achar a fonte, recusar em
vez de adivinhar, parar, atualizar, e subir de volta MESMO SE algo no meio
falhar. Um update que quebra pela metade nao pode deixar a maquina sem servico.

Nada aqui toca pip, git, servico ou rede de verdade.
"""
from __future__ import annotations

import json
import sys

import pytest

from delegation_core import installer


# ── de onde este install veio ───────────────────────────────────────────────

def _dist_info_falso(monkeypatch, tmp_path, conteudo):
    info = tmp_path / "delegation_core-0.13.0.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    if conteudo is not None:
        (info / "direct_url.json").write_text(json.dumps(conteudo), encoding="utf-8")
    monkeypatch.setattr(installer, "_dist_info", lambda: info)
    return info


def test_le_a_fonte_do_direct_url(monkeypatch, tmp_path):
    """PEP 610: o pip grava de onde instalou. Nao se adivinha por __file__."""
    fonte = tmp_path / "checkout"; fonte.mkdir()
    _dist_info_falso(monkeypatch, tmp_path, {"url": f"file://{fonte}", "dir_info": {}})
    assert installer.source_root() == fonte


def test_reconhece_instalacao_editavel(monkeypatch, tmp_path):
    fonte = tmp_path / "checkout"; fonte.mkdir()
    _dist_info_falso(monkeypatch, tmp_path,
                     {"url": f"file://{fonte}", "dir_info": {"editable": True}})
    assert installer.is_editable() is True


def test_instalado_de_um_indice_nao_tem_fonte(monkeypatch, tmp_path):
    """`pip install delegation-core` de um indice nao da diretorio de origem."""
    _dist_info_falso(monkeypatch, tmp_path,
                     {"url": "https://pypi.org/simple/delegation-core", "dir_info": {}})
    assert installer.source_root() is None


def test_direct_url_ausente_ou_corrompido_nao_explode(monkeypatch, tmp_path):
    _dist_info_falso(monkeypatch, tmp_path, None)
    assert installer.source_root() is None
    info = _dist_info_falso(monkeypatch, tmp_path, {"url": "file:///x", "dir_info": {}})
    (info / "direct_url.json").write_text("{nao e json", encoding="utf-8")
    assert installer.source_root() is None


def test_fonte_que_sumiu_do_disco_nao_e_devolvida(monkeypatch, tmp_path):
    """A pasta pode ter sido apagada depois da instalacao."""
    _dist_info_falso(monkeypatch, tmp_path,
                     {"url": f"file://{tmp_path / 'ja_nao_existe'}", "dir_info": {}})
    assert installer.source_root() is None


# ── docs e hooks: o defeito que a comparacao corrige ────────────────────────

@pytest.fixture
def arvore(tmp_path, monkeypatch):
    raiz = tmp_path / "src"; (raiz / "hooks").mkdir(parents=True)
    (raiz / "AGENT_GUIDE.md").write_text("guia v2\n", encoding="utf-8")
    (raiz / "CLAUDE_SYSTEM_PROMPT.md").write_text("prompt v2\n", encoding="utf-8")
    (raiz / "hooks" / "session_export.py").write_text("# hook v2\n", encoding="utf-8")
    cfg = tmp_path / "cfg"; cfg.mkdir()
    monkeypatch.setattr(installer, "CONFIG_DIR", cfg)
    return raiz, cfg


def test_primeira_instalacao_copia(arvore):
    raiz, cfg = arvore
    r = installer.refresh_shipped_files(raiz)
    assert "AGENT_GUIDE.md" in r["installed"]
    assert (cfg / "AGENT_GUIDE.md").read_text() == "guia v2\n"


def test_arquivo_IDENTICO_nao_vira_customizacao(arvore):
    """O defeito. install.sh escrevia .dist.md sempre que o destino EXISTIA.

    Medido nesta maquina antes da correcao: AGENT_GUIDE.md e AGENT_GUIDE.dist.md
    byte a byte iguais em 35.857 bytes, e o par do CLAUDE_SYSTEM_PROMPT em
    6.148. O usuario era informado de que tinha customizado dois arquivos que
    nunca tocou, e ficava com copias redundantes para reconciliar.
    """
    raiz, cfg = arvore
    (cfg / "AGENT_GUIDE.md").write_text("guia v2\n", encoding="utf-8")   # identico

    r = installer.refresh_shipped_files(raiz)

    assert "AGENT_GUIDE.md" in r["unchanged"]
    assert "AGENT_GUIDE.md" not in r["kept_yours"]
    assert not (cfg / "AGENT_GUIDE.dist.md").exists(), "escreveu .dist para arquivo identico"


def test_arquivo_REALMENTE_customizado_e_preservado(arvore):
    raiz, cfg = arvore
    (cfg / "AGENT_GUIDE.md").write_text("meu guia traduzido\n", encoding="utf-8")

    r = installer.refresh_shipped_files(raiz)

    assert "AGENT_GUIDE.md" in r["kept_yours"]
    assert (cfg / "AGENT_GUIDE.md").read_text() == "meu guia traduzido\n", "clobberou o do usuario"
    assert (cfg / "AGENT_GUIDE.dist.md").read_text() == "guia v2\n"


def test_hooks_nao_sao_mais_copiados(arvore):
    """Desde a v0.15.0 os hooks rodam do pacote e sao registrados, nao copiados
    (clients.register_session_hooks). Uma arvore que ainda tenha hooks/ nao pode
    fazer o refresh voltar a copiar."""
    raiz, cfg = arvore

    r = installer.refresh_shipped_files(raiz)

    assert not any("hooks/" in x for lista in r.values() for x in lista)
    assert not (cfg / "hooks").exists()


def test_arquivo_que_nao_veio_no_pacote_e_reportado(arvore):
    raiz, _ = arvore
    (raiz / "CLAUDE_SYSTEM_PROMPT.md").unlink()
    assert "CLAUDE_SYSTEM_PROMPT.md" in installer.refresh_shipped_files(raiz)["missing"]


def test_acha_as_sobras_dist_identicas(arvore):
    _, cfg = arvore
    (cfg / "AGENT_GUIDE.md").write_text("igual\n", encoding="utf-8")
    (cfg / "AGENT_GUIDE.dist.md").write_text("igual\n", encoding="utf-8")
    (cfg / "CLAUDE_SYSTEM_PROMPT.md").write_text("a\n", encoding="utf-8")
    (cfg / "CLAUDE_SYSTEM_PROMPT.dist.md").write_text("b\n", encoding="utf-8")

    sobras = installer.stale_dist_copies()

    assert sobras == ["AGENT_GUIDE.dist.md"], "reportou um .dist que difere de verdade"


# ── update: a ordem e o que importa ─────────────────────────────────────────

@pytest.fixture
def cenario(tmp_path, monkeypatch):
    """Fonte, servico e pip falsos, com um diario da ordem das chamadas."""
    raiz = tmp_path / "src"; (raiz / "hooks").mkdir(parents=True)
    (raiz / "AGENT_GUIDE.md").write_text("guia\n", encoding="utf-8")
    monkeypatch.setattr(installer, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(installer, "source_root", lambda: raiz)
    monkeypatch.setattr(installer, "is_editable", lambda: False)
    monkeypatch.setattr(installer, "git_state", lambda _: {"git": False})

    diario: list[str] = []
    estado = {"no_ar": True, "pip_ok": True, "stop_ok": True}

    class _ServicoFalso:
        @staticmethod
        def is_up(wait_seconds=0.0):
            diario.append("is_up"); return estado["no_ar"]

        @staticmethod
        def stop(timeout=None):
            diario.append("stop")
            if not estado["stop_ok"]:
                return {"status": "failed", "detail": "nao morreu"}
            estado["no_ar"] = False
            return {"status": "stopped", "detail": ""}

        @staticmethod
        def start():
            diario.append("start"); estado["no_ar"] = True
            return {"status": "started", "detail": ""}

        @staticmethod
        def install():
            diario.append("register"); return {"status": "installed"}

    def _pip(root):
        diario.append("pip")
        return (True, "ok") if estado["pip_ok"] else (False, "ERRO: no space left")

    monkeypatch.setattr(installer, "service", _ServicoFalso)
    monkeypatch.setattr(installer, "_pip_install", _pip)
    # O fim do update roda num interpretador novo, onde estes dubles nao
    # existiriam. Aqui ele roda no mesmo processo; o filho tem testes proprios.
    monkeypatch.setattr(installer, "_finish_in_new_code", installer.finish_update)
    return {"raiz": raiz, "diario": diario, "estado": estado}


def test_para_o_daemon_ANTES_do_pip(cenario):
    """A razao de este comando existir."""
    installer.update()
    d = cenario["diario"]
    assert d.index("stop") < d.index("pip"), f"pip rodou com o daemon no ar: {d}"


def test_sobe_de_volta_depois_de_registrar(cenario):
    installer.update()
    d = cenario["diario"]
    assert d.index("register") < d.index("start")
    assert cenario["estado"]["no_ar"] is True


def test_pip_que_falha_ainda_sobe_o_daemon(cenario):
    """Um update quebrado nao pode deixar a maquina sem servico."""
    cenario["estado"]["pip_ok"] = False

    r = installer.update()

    assert r["status"] == "failed"
    assert "no space left" in r["detail"]
    assert "start" in cenario["diario"], "deixou o daemon parado apos falhar"
    assert cenario["estado"]["no_ar"] is True


def test_stop_que_falha_aborta_antes_do_pip(cenario):
    """Escrever no venv sob um daemon vivo e o que se quer evitar."""
    cenario["estado"]["stop_ok"] = False

    r = installer.update()

    assert r["status"] == "failed"
    assert "pip" not in cenario["diario"]


def test_daemon_que_ja_estava_parado_nao_e_ligado(cenario):
    """Update nao e o comando para iniciar um servico que o usuario desligou."""
    cenario["estado"]["no_ar"] = False
    installer.update()
    assert "start" not in cenario["diario"]


def test_no_restart_deixa_parado(cenario):
    installer.update(restart=False)
    assert "start" not in cenario["diario"]
    assert cenario["estado"]["no_ar"] is False


def test_check_only_nao_toca_em_nada(cenario):
    r = installer.update(check_only=True)
    assert r["status"] == "unknown_not_a_checkout"
    assert cenario["diario"] == [], f"check tocou no sistema: {cenario['diario']}"


def test_sem_fonte_recusa_com_explicacao(monkeypatch):
    monkeypatch.setattr(installer, "source_root", lambda: None)
    r = installer.update()
    assert r["status"] == "cannot_locate_source"
    assert "installer" in r["detail"].lower()


def test_checkout_sujo_e_recusado(cenario, monkeypatch):
    """Um pull sobre trabalho nao commitado pode conflitar ou descartar."""
    monkeypatch.setattr(installer, "git_state", lambda _: {
        "git": True, "dirty": True, "dirty_files": 3, "branch": "x", "commit": "y"})

    r = installer.update()

    assert r["status"] == "refused_dirty_checkout"
    assert "3 uncommitted" in r["detail"]
    assert cenario["diario"] == [], "mexeu no sistema antes de recusar"


def test_pull_que_falha_nao_reinstala(cenario, monkeypatch):
    monkeypatch.setattr(installer, "git_state", lambda _: {"git": True, "dirty": False})
    monkeypatch.setattr(installer, "_git", lambda *a, **k: (1, "diverged"))

    r = installer.update()

    assert r["status"] == "failed"
    assert "pip" not in cenario["diario"]
    assert "ff-only" in r["detail"]


def test_daemon_que_nao_responde_apos_subir_e_reportado(cenario, monkeypatch):
    """`start` devolve quando o gerenciador aceita, nao quando o daemon atende."""
    class _NaoAtende(installer.service):
        @staticmethod
        def is_up(wait_seconds=0.0):
            return wait_seconds == 0.0      # estava no ar antes, nao responde depois

    monkeypatch.setattr(installer, "service", _NaoAtende)
    r = installer.update()
    assert r["status"] == "started_but_not_answering"
    assert "60s" in r["detail"]


# ── o fim do update roda no codigo novo ─────────────────────────────────────

class _Processo:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_fim_do_update_roda_num_interpretador_novo(monkeypatch, tmp_path):
    """Em 03/10 o update para a v0.15 rodou os passos da v0.14, e os hooks
    novos nao foram registrados. O fim tem que vir do codigo recem-instalado."""
    chamadas = []
    passos = [{"step": "register_hooks", "ok": True}]

    def _run(cmd, **kw):
        chamadas.append(cmd)
        return _Processo(stdout="aviso de import qualquer\n"
                                + installer.FINISH_MARKER + json.dumps(passos) + "\n")

    monkeypatch.setattr(installer.subprocess, "run", _run)
    monkeypatch.setattr(installer, "finish_update",
                        lambda root: pytest.fail("rodou os passos no codigo velho"))

    r = installer._finish_in_new_code(tmp_path)

    assert chamadas[0][:3] == [installer.sys.executable, "-m", "delegation_core"]
    assert chamadas[0][3] == "update-finish"
    assert r == [{"step": "finish_in_new_code", "ok": True}] + passos


@pytest.mark.parametrize("processo", [
    _Processo(returncode=2, stderr="invalid choice: 'update-finish'"),
    _Processo(returncode=0, stdout="sem a linha marcada\n"),
])
def test_filho_que_falha_cai_para_o_processo_atual(monkeypatch, tmp_path, processo):
    """Versao antiga sem `update-finish`, ou filho que nao responde: os passos
    rodam aqui mesmo, como antes, e o relatorio diz que foi assim."""
    monkeypatch.setattr(installer.subprocess, "run", lambda cmd, **kw: processo)
    monkeypatch.setattr(installer, "finish_update",
                        lambda root: [{"step": "register_hooks", "ok": True}])

    r = installer._finish_in_new_code(tmp_path)

    assert r[0]["step"] == "finish_in_new_code" and r[0]["ok"] is False
    assert r[0]["detail"]
    assert r[1] == {"step": "register_hooks", "ok": True}


def test_update_finish_imprime_a_linha_marcada(monkeypatch, tmp_path, capsys):
    """O lado do filho: o comando escondido devolve os passos numa linha so."""
    from delegation_core import cli

    monkeypatch.setattr(installer, "finish_update",
                        lambda root: [{"step": "refresh_docs", "ok": True, "root": str(root)}])
    monkeypatch.setattr(sys, "argv", ["delegation-core", "update-finish", "--root", str(tmp_path)])

    try:
        codigo = cli.main()
    except SystemExit as e:
        codigo = e.code

    saida = capsys.readouterr().out.splitlines()
    marcada = [l for l in saida if l.startswith(installer.FINISH_MARKER)]
    assert codigo in (0, None)
    assert json.loads(marcada[-1][len(installer.FINISH_MARKER):])[0]["root"] == str(tmp_path)



def test_update_chama_o_fim_no_codigo_novo_depois_do_pip(cenario, monkeypatch):
    """Sem isto, um update() que voltasse a chamar finish_update direto
    passaria em todos os testes acima."""
    def _fim(root):
        cenario["diario"].append("fim_novo")
        return []

    monkeypatch.setattr(installer, "_finish_in_new_code", _fim)
    installer.update()
    d = cenario["diario"]
    assert d.index("pip") < d.index("fim_novo") < d.index("start")
