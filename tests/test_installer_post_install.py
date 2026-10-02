"""O que install.sh e install.bat faziam depois do `pip install`, agora uma vez so.

As duas metades tinham divergido, e a divergencia tem consequencia:

    install.bat, no upgrade:  service install  +  clients --claude-code
    install.sh,  no upgrade:  service install

Ou seja, o mesmo upgrade atualizava a configuracao do cliente MCP no Windows e
a deixava intacta no Linux e no macOS.

E nenhum dos dois chamava `clients --claude-desktop`, que passou a importar
muito mais depois do relatorio de campo de 03/09: escrita na forma do Claude
Code, a entrada do Desktop carrega um `url`, e o Desktop responde a isso
reescrevendo o arquivo e descartando a secao `mcpServers` INTEIRA. Quem
atualizasse para o codigo que corrige isso continuaria carregando a entrada que
causa o problema, porque o instalador nunca a reescrevia.

Nada aqui toca a maquina: sem sudo, sem gh, sem dpkg, sem hdiutil, sem
~/.claude, sem gerenciador de servico. O `conftest.py` ja reaponta CONFIG_DIR.
"""
from __future__ import annotations

import json

import pytest

from delegation_core import installer


@pytest.fixture
def checkout(tmp_path):
    """Uma copia falsa do repositorio, com docs, hooks e skills."""
    raiz = tmp_path / "checkout"
    (raiz / "hooks").mkdir(parents=True)
    (raiz / "skills" / "uma-skill").mkdir(parents=True)
    (raiz / "skills" / "uma-skill" / "SKILL.md").write_text("# skill")
    (raiz / "skills" / "outra-skill").mkdir(parents=True)
    (raiz / "skills" / "outra-skill" / "SKILL.md").write_text("# outra")
    (raiz / "agents").mkdir(parents=True)
    (raiz / "agents" / "um-agente.md").write_text("---\nname: um-agente\n---\n")
    for nome in installer.SHIPPED_DOCS:
        (raiz / nome).write_text(f"# {nome}")
    (raiz / "hooks" / "session_export.py").write_text("# hook")
    return raiz


@pytest.fixture
def sem_maquina(monkeypatch, tmp_path):
    """Neutraliza tudo que sairia deste processo, e registra o que foi chamado."""
    registro: dict = {"chamadas": []}

    def _dashboard(root):
        registro["chamadas"].append("dashboard")
        return {"method": None, "status": "not_found", "detail": "fake"}

    def _service_install():
        registro["chamadas"].append("service.install")
        return {"platform": "Linux", "status": "installed"}

    monkeypatch.setattr(installer, "install_dashboard", _dashboard)
    monkeypatch.setattr(installer.service, "install", _service_install)
    monkeypatch.setattr(installer.Path, "home", classmethod(lambda cls: tmp_path / "casa"))
    (tmp_path / "casa").mkdir(exist_ok=True)
    return registro


# ── a divergencia entre os dois instaladores ────────────────────────────────

def test_o_upgrade_reconfigura_o_cliente_em_TODAS_as_plataformas(
        checkout, sem_maquina, _sem_escrita_no_estado_real, monkeypatch):
    """O `.sh` nao fazia isto e o `.bat` fazia. Agora e o mesmo codigo."""
    cfg_dir = _sem_escrita_no_estado_real
    (cfg_dir / "config.json").write_text(json.dumps({"vault_path": "/tmp/v"}))

    chamados = []
    monkeypatch.setattr(installer, "repair_client_configs",
                        lambda: chamados.append("clients") or {"claude_code": {"status": "ok"}})

    r = installer.post_install(checkout)
    assert r["fresh_install"] is False
    assert chamados == ["clients"]
    assert "service.install" in sem_maquina["chamadas"]


def test_uma_instalacao_nova_NAO_reconfigura_cliente_nem_servico(
        checkout, sem_maquina, _sem_escrita_no_estado_real, monkeypatch):
    """Sem config.json nao ha nada para registrar nem reparar: o wizard e quem
    escreve a config que os dois leem."""
    monkeypatch.setattr(installer, "repair_client_configs",
                        lambda: pytest.fail("reconfigurou cliente antes do wizard"))
    r = installer.post_install(checkout)

    assert r["fresh_install"] is True
    assert r["needs_wizard"] is True
    assert "service.install" not in sem_maquina["chamadas"]
    assert "service" not in r


def test_quem_decide_rodar_o_wizard_e_o_shell_e_nao_este_codigo(
        checkout, sem_maquina, _sem_escrita_no_estado_real):
    """`post_install` REPORTA que o wizard e preciso. Um programa que entrega o
    terminal a outro interativo tem que ser o que tem o terminal."""
    import inspect
    fonte = inspect.getsource(installer.post_install)
    assert "needs_wizard" in fonte
    assert "run_wizard" not in fonte and "wizard.run" not in fonte


# ── o reparo do Claude Desktop ──────────────────────────────────────────────

@pytest.fixture
def clientes_falsos(monkeypatch, tmp_path):
    from delegation_core import clients

    alvo = tmp_path / "claude_desktop_config.json"
    registro = {"code": 0, "desktop": 0}

    monkeypatch.setattr(clients, "claude_desktop_config_path", lambda: alvo)
    monkeypatch.setattr(clients, "install_claude_code",
                        lambda cfg: registro.__setitem__("code", registro["code"] + 1)
                        or {"status": "installed"})
    monkeypatch.setattr(clients, "install_claude_desktop",
                        lambda cfg, **k: registro.__setitem__("desktop", registro["desktop"] + 1)
                        or {"status": "installed", "repaired_unsafe_url_entry": True})

    from delegation_core.config import Config
    monkeypatch.setattr(Config, "load", classmethod(
        lambda cls: Config(vault_path=str(tmp_path), server_token="t", server_port=8787)))
    return registro, alvo


def test_repara_a_entrada_do_desktop_quando_ela_existe(clientes_falsos):
    registro, alvo = clientes_falsos
    alvo.write_text(json.dumps({"mcpServers": {
        "delegation-core": {"type": "http", "url": "http://127.0.0.1:8787/mcp"}}}))

    r = installer.repair_client_configs()
    assert registro["desktop"] == 1
    assert r["claude_desktop"]["repaired_unsafe_url_entry"] is True


def test_NAO_cria_entrada_no_desktop_para_quem_nunca_configurou(clientes_falsos):
    """Um instalador que se acrescenta a um cliente que o usuario nao pediu e
    outra coisa, nao um reparo."""
    registro, alvo = clientes_falsos
    alvo.write_text(json.dumps({"mcpServers": {"clickup": {"command": "npx"}}}))

    r = installer.repair_client_configs()
    assert registro["desktop"] == 0
    assert r["claude_desktop"]["status"] == "not_configured"
    assert "clients --claude-desktop" in r["claude_desktop"]["detail"]


def test_sem_arquivo_do_desktop_tambem_nao_cria(clientes_falsos):
    registro, alvo = clientes_falsos
    assert not alvo.exists()
    assert installer.repair_client_configs()["claude_desktop"]["status"] == "not_configured"


def test_json_invalido_do_desktop_nao_vira_reparo_as_cegas(clientes_falsos):
    registro, alvo = clientes_falsos
    alvo.write_text("{nao e json")
    assert installer.repair_client_configs()["claude_desktop"]["status"] == "not_configured"
    assert registro["desktop"] == 0


def test_o_claude_code_e_sempre_reconfigurado(clientes_falsos):
    """Diferente do Desktop: a entrada do Code e a forma http, que e correta e
    que o daemon precisa que aponte para a porta certa."""
    registro, _ = clientes_falsos
    installer.repair_client_configs()
    assert registro["code"] == 1


def test_uma_falha_num_cliente_nao_derruba_o_outro(clientes_falsos, monkeypatch):
    from delegation_core import clients
    monkeypatch.setattr(clients, "install_claude_code",
                        lambda cfg: (_ for _ in ()).throw(RuntimeError("boom")))
    r = installer.repair_client_configs()
    assert r["claude_code"]["status"] == "error"
    assert "claude_desktop" in r


# ── skills ──────────────────────────────────────────────────────────────────

def test_instala_as_skills_empacotadas(checkout, sem_maquina, tmp_path):
    r = installer.install_skills(checkout)
    assert sorted(r["installed"]) == ["outra-skill", "uma-skill"]
    assert (tmp_path / "casa" / ".claude" / "skills" / "uma-skill" / "SKILL.md").is_file()


def test_nunca_sobrescreve_uma_skill_que_o_usuario_ja_tem(checkout, sem_maquina, tmp_path):
    minha = tmp_path / "casa" / ".claude" / "skills" / "uma-skill"
    minha.mkdir(parents=True)
    (minha / "SKILL.md").write_text("a minha versao")

    r = installer.install_skills(checkout)
    assert r["kept_yours"] == ["uma-skill"]
    assert (minha / "SKILL.md").read_text() == "a minha versao"


def test_checkout_sem_pasta_de_skills_nao_e_erro(tmp_path, sem_maquina):
    vazio = tmp_path / "vazio"
    vazio.mkdir()
    r = installer.install_skills(vazio)
    assert r["available"] is False
    assert r["installed"] == []


# ── agents ──────────────────────────────────────────────────────────────────

def test_instala_os_agentes_empacotados(checkout, sem_maquina, tmp_path):
    r = installer.install_agents(checkout)
    assert r["installed"] == ["um-agente"]
    assert (tmp_path / "casa" / ".claude" / "agents" / "um-agente.md").is_file()


def test_nunca_sobrescreve_um_agente_que_o_usuario_ja_tem(checkout, sem_maquina, tmp_path):
    # Um subagente que a pessoa afinou para o proprio fluxo nao pode ser
    # trocado em silencio por um que veio no pacote.
    meu = tmp_path / "casa" / ".claude" / "agents"
    meu.mkdir(parents=True)
    (meu / "um-agente.md").write_text("a minha versao")

    r = installer.install_agents(checkout)
    assert r["kept_yours"] == ["um-agente"]
    assert (meu / "um-agente.md").read_text() == "a minha versao"


def test_checkout_sem_pasta_de_agentes_nao_e_erro(tmp_path, sem_maquina):
    vazio = tmp_path / "vazio"
    vazio.mkdir()
    r = installer.install_agents(vazio)
    assert r["available"] is False
    assert r["installed"] == []


def test_post_install_relata_os_agentes(checkout, sem_maquina):
    assert installer.post_install(checkout)["agents"]["installed"] == ["um-agente"]


# ── o slug do repositorio ───────────────────────────────────────────────────

@pytest.mark.parametrize("url,esperado", [
    ("https://github.com/AnonJoey/delegation-core-v7.git", "AnonJoey/delegation-core-v7"),
    ("https://github.com/AnonJoey/delegation-core-v7", "AnonJoey/delegation-core-v7"),
    ("git@github.com:AnonJoey/delegation-core-v7.git", "AnonJoey/delegation-core-v7"),
])
def test_le_o_slug_do_remote(monkeypatch, tmp_path, url, esperado):
    monkeypatch.setattr(installer, "_git", lambda root, *a, **k: (0, url))
    assert installer.repo_slug(tmp_path) == esperado


def test_sem_remote_usa_o_padrao(monkeypatch, tmp_path):
    monkeypatch.setattr(installer, "_git", lambda root, *a, **k: (1, ""))
    assert installer.repo_slug(tmp_path) == installer.DEFAULT_REPO_SLUG


def test_remote_que_nao_e_do_github_nao_vira_slug(monkeypatch, tmp_path):
    monkeypatch.setattr(installer, "_git", lambda root, *a, **k: (0, "https://gitlab.com/x/y.git"))
    assert installer.repo_slug(tmp_path) == installer.DEFAULT_REPO_SLUG


@pytest.mark.parametrize("url", [
    "https://github.com/AnonJoey/delegation-core-v7/tree/master",
    "https://github.com/AnonJoey",
    "https://github.com/",
])
def test_url_do_github_malformada_cai_no_padrao(monkeypatch, tmp_path, url):
    """O slug vai direto para `gh release download --repo`. Um valor com barra
    a mais ou a menos nao e um repositorio, e mandar isso para o gh produz um
    erro sobre um repo que o usuario nunca digitou."""
    monkeypatch.setattr(installer, "_git", lambda root, *a, **k: (0, url))
    assert installer.repo_slug(tmp_path) == installer.DEFAULT_REPO_SLUG


# ── o dashboard nunca derruba a instalacao ──────────────────────────────────

def test_dashboard_ausente_nao_e_falha_da_instalacao(
        checkout, _sem_escrita_no_estado_real, monkeypatch, tmp_path):
    monkeypatch.setattr(installer.Path, "home", classmethod(lambda cls: tmp_path / "casa"))
    (tmp_path / "casa").mkdir(exist_ok=True)
    monkeypatch.setattr(installer.shutil, "which", lambda nome: None)

    r = installer.post_install(checkout)
    assert r["dashboard"]["status"] in ("not_found", "unsupported")
    assert r["docs_and_hooks"]["installed"], "parou antes de instalar os docs"


def test_uma_excecao_no_dashboard_vira_status_e_nao_propaga(monkeypatch, tmp_path):
    monkeypatch.setattr(installer, "_dashboard_artifact",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disco cheio")))
    r = installer.install_dashboard(tmp_path)
    assert r["status"] == "failed"
    assert "disco cheio" in r["detail"]


def test_o_cache_de_saude_e_invalidado(checkout, sem_maquina, _sem_escrita_no_estado_real):
    cfg_dir = _sem_escrita_no_estado_real
    (cfg_dir / "vault_health.json").write_text('{"broken_links": 999}')
    r = installer.post_install(checkout)
    assert r["health_cache"] == "invalidated"
    assert not (cfg_dir / "vault_health.json").exists()
