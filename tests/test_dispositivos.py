"""Cada modelo roda onde a config manda, e o arbitro so cuida de quem esta na GPU.

Dois residentes disputavam uma placa de 16 GB, e `gpu.py` nasceu para alternar
os dois. Depois que o BGE foi para a CPU (`embed_device`), o arbitro continuou
sendo chamado como se os dois estivessem na placa: toda abertura ou reabertura
do indice fazia `gpu.take("embeddings")`, que derruba o llama-server. A
reabertura acontece sempre que outro processo escreve no indice, entao uma
tarefa local em andamento podia morrer no meio por causa de um encoder que nem
usa a GPU.

O modelo local nao tinha como ir para a CPU de proposito: `llama_ngl <= 0`
quer dizer "o llama.cpp encaixa o que couber", e o unico jeito de forcar CPU
era esconder a placa do daemon inteiro, o drop-in que em 26/09 deixou o modelo
a 5,5 tokens/s com a GPU ociosa porque o filho herdava o esconderijo.
"""

from __future__ import annotations

import json

import pytest

from delegation_core import engine as engine_mod
from delegation_core import gpu
from delegation_core.config import Config
from delegation_core.vault import VaultManager


# ── vault: BGE na CPU nao mexe no arbitro ────────────────────────────────────


class _Embedder:
    def __call__(self, input):
        return [[1.0] + [0.0] * 7 for _ in input]

    def embed_documents(self, input):
        return self(input)

    def embed_query(self, input):
        return self([input])[0] if isinstance(input, str) else self(list(input))

    def name(self):
        return "dispositivos"


def _abre_vault(tmp_path, monkeypatch, embed_device):
    chamadas = []
    monkeypatch.setattr(gpu, "take", lambda quem: chamadas.append(quem) or 0)
    cfg = Config(vault_path=str(tmp_path / "vault"), vault_folders=["Notes"],
                 embed_device=embed_device)
    (tmp_path / "vault" / "Notes").mkdir(parents=True)
    import delegation_core.vault as vault_mod
    monkeypatch.setattr(vault_mod, "make_bge_embedding_function", lambda *a, **k: _Embedder())
    vm = VaultManager(cfg)
    vm._init()
    assert vm._initialized
    vm._close_client()
    return chamadas


def test_bge_na_cpu_nao_pede_a_placa(tmp_path, monkeypatch):
    assert _abre_vault(tmp_path, monkeypatch, "cpu") == []


def test_bge_na_gpu_pede_a_placa(tmp_path, monkeypatch):
    assert _abre_vault(tmp_path, monkeypatch, "cuda") == ["embeddings"]


# ── engine: onde o modelo local roda ─────────────────────────────────────────


class _Popen:
    vistos: list = []

    def __init__(self, cmd, **kw):
        _Popen.vistos.append((cmd, kw))
        self.pid = 4242

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


@pytest.fixture
def lanca(tmp_path, monkeypatch):
    binario = tmp_path / "llama-server"
    modelo = tmp_path / "modelo.gguf"
    binario.write_text("")
    modelo.write_text("")
    tomadas = []
    monkeypatch.setattr(gpu, "take", lambda quem: tomadas.append(quem) or 0)
    monkeypatch.setattr(engine_mod.subprocess, "Popen", _Popen)
    monkeypatch.setattr(engine_mod.time, "sleep", lambda s: None)
    _Popen.vistos = []

    def _lanca(**campos):
        cfg = Config(llama_binary=str(binario), llama_model=str(modelo), **campos)
        eng = engine_mod.DelegationEngine(cfg)
        eng._is_healthy = lambda: True
        assert eng._start()
        cmd, kw = _Popen.vistos[-1]
        return cmd, kw.get("env"), tomadas

    return _lanca


def _camadas(cmd):
    return cmd[cmd.index("--n-gpu-layers") + 1] if "--n-gpu-layers" in cmd else None


def test_modelo_na_cpu_tem_zero_camadas_e_placa_escondida_so_no_filho(lanca, monkeypatch):
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    cmd, env, tomadas = lanca(llama_device="cpu", llama_ngl=999)
    assert _camadas(cmd) == "0"
    assert env["CUDA_VISIBLE_DEVICES"] == ""
    assert tomadas == [], "modelo na CPU nao despeja o BGE da placa"
    import os
    assert "CUDA_VISIBLE_DEVICES" not in os.environ, "so o filho perde a placa"


def test_modelo_na_gpu_nao_herda_placa_escondida(lanca, monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    cmd, env, tomadas = lanca(llama_device="gpu", llama_ngl=0)
    assert "CUDA_VISIBLE_DEVICES" not in env
    assert _camadas(cmd) is None, "ngl <= 0 continua deixando o llama.cpp encaixar"
    assert tomadas == ["llama"]


def test_auto_mantem_o_comportamento_de_antes(lanca, monkeypatch):
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    cmd, env, tomadas = lanca(llama_ngl=999)
    assert _camadas(cmd) == "999"
    assert tomadas == ["llama"]


def test_valor_desconhecido_vira_auto_com_aviso(caplog):
    cfg = Config(llama_device="tpu")
    with caplog.at_level("WARNING"):
        assert engine_mod.llama_device(cfg) == "auto"
    assert "llama_device" in caplog.text


# ── o que o sistema diz sobre onde cada um roda ──────────────────────────────


def test_heartbeat_informa_os_dispositivos():
    import delegation_core.server as server

    cfg = Config(embed_device="cpu", llama_device="gpu")
    dispositivos = server._dispositivos(cfg)
    assert dispositivos == {"embeddings": "cpu", "modelo_local": "gpu"}


def test_doctor_avisa_quando_os_dois_disputam_a_placa():
    from delegation_core import doctor

    disputa = doctor.check_devices(Config(embed_device="cuda", llama_device="gpu"),
                                   tem_cuda=lambda: True)
    assert disputa["status"] == "warn"
    separado = doctor.check_devices(Config(embed_device="cpu", llama_device="gpu"),
                                    tem_cuda=lambda: True)
    assert separado["status"] == "ok"
    assert "embeddings: cpu" in separado["detail"]
    assert "modelo local: gpu" in separado["detail"]


def test_o_check_de_dispositivos_esta_no_run_all():
    import inspect

    from delegation_core import doctor
    assert "check_devices(" in inspect.getsource(doctor.run_all)


def test_dashboard_aceita_so_valores_conhecidos():
    from delegation_core import dashboard_api
    assert dashboard_api.DISPOSITIVOS_DO_MODELO == ("auto", "gpu", "cpu")
    json.dumps(dashboard_api.DISPOSITIVOS_DO_MODELO)


def test_a_lista_do_dashboard_e_a_do_engine():
    from delegation_core import dashboard_api
    assert dashboard_api.DISPOSITIVOS_DO_MODELO == engine_mod.LLAMA_DEVICES
