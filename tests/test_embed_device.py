"""Escolher em que dispositivo o encoder roda, sem esconder a placa do processo.

## Por que este campo existe

Nesta maquina o BGE-m3 e o llama-server nao cabem juntos nos 16 GB da placa: o
BGE segurava 3364 MiB e o segundo a carregar morria em `cudaMalloc`. Desde
09/09/2026 a solucao em uso e um drop-in de systemd com
`CUDA_VISIBLE_DEVICES=`, e o comentario do proprio drop-in diz por que foi
preciso chegar la: *"detect_device() escolhe cuda sempre que
torch.cuda.is_available(), e nao le nenhuma variavel propria, entao esconder a
placa e o unico jeito sem tocar no codigo"*.

Esconder a placa apaga a GPU para tudo que rodar naquele processo, nao so para
o encoder. Um campo de config faz a mesma coisa com pontaria.

## O que estes testes guardam

A separacao de papeis, que e o ponto do desenho: `detect_device()` responde o
que a maquina TEM, `resolver_dispositivo()` responde o que se QUER. Confundir
as duas e o que produziu o workaround do systemd.
"""

from __future__ import annotations

import pytest

from delegation_core import embeddings
from delegation_core.config import Config


def test_auto_e_o_comportamento_historico(monkeypatch):
    """Sem preferencia, quem decide e a deteccao de hardware. Nada muda."""
    monkeypatch.setattr(embeddings, "detect_device", lambda: "cuda")
    assert embeddings.resolver_dispositivo("auto") == "cuda"
    assert embeddings.resolver_dispositivo(None) == "cuda"
    assert embeddings.resolver_dispositivo("") == "cuda"


def test_cpu_vence_a_placa_disponivel(monkeypatch):
    """O caso que motivou o campo: ha CUDA e mesmo assim se quer CPU.

    E a escolha legitima de quem precisa da placa para o llama-server.
    """
    monkeypatch.setattr(embeddings, "detect_device",
                        lambda: pytest.fail("com preferencia explicita nao se detecta"))
    assert embeddings.resolver_dispositivo("cpu") == "cpu"


@pytest.mark.parametrize("pedido", ["cuda", "mps", "cpu"])
def test_cada_dispositivo_conhecido_passa_intacto(monkeypatch, pedido):
    monkeypatch.setattr(embeddings, "detect_device", lambda: "outro")
    assert embeddings.resolver_dispositivo(pedido) == pedido


def test_maiuscula_e_espaco_nao_estragam(monkeypatch):
    """Valor vem de JSON editado a mao; ' CPU ' e a mesma intencao."""
    monkeypatch.setattr(embeddings, "detect_device", lambda: "cuda")
    assert embeddings.resolver_dispositivo("  CPU  ") == "cpu"


def test_valor_desconhecido_cai_em_auto_e_nao_levanta(monkeypatch, caplog):
    """Erro de digitacao no config nao pode deixar a busca sem embedder.

    Levantar aqui seria trocar 'o encoder foi para o lugar errado' por 'nao ha
    encoder nenhum', que e estritamente pior.
    """
    monkeypatch.setattr(embeddings, "detect_device", lambda: "cuda")
    with caplog.at_level("WARNING"):
        assert embeddings.resolver_dispositivo("gpu") == "cuda"
    assert "gpu" in caplog.text


def test_o_campo_existe_no_config_e_o_padrao_nao_muda_nada():
    assert Config().embed_device == "auto"


def test_o_vault_entrega_a_preferencia_ao_encoder(monkeypatch):
    """A LIGACAO, nao a funcao.

    `embed_max_seq_length` e `embed_batch_size` ja tinham existido inertes no
    Config antes de alguem os passar adiante, e o comentario no `vault.py`
    registra isso: "os limites sao passados aqui ou em lugar nenhum". Um campo
    que nao chega ao encoder e exatamente o mesmo defeito outra vez.

    Conferido por mutacao: tirar `device=` da chamada em `vault.py` faz este
    teste falhar.
    """
    from delegation_core import vault as vault_mod

    recebido = {}

    class _Parar(Exception):
        """Aborta o _init logo depois do ponto medido."""

    def falso(model_name, max_seq_length=None, batch_size=None, device="auto"):
        # A assinatura acompanha a real de proposito: um duble que aceitasse
        # **kwargs deixaria verde um `device=` que a funcao verdadeira recusa.
        recebido["device"] = device
        raise _Parar

    monkeypatch.setattr(vault_mod, "make_bge_embedding_function", falso)

    cfg = Config(vault_path="/tmp/vault-de-teste", embed_device="cpu")
    vm = vault_mod.VaultManager(cfg)
    try:
        vm._init()
    except Exception:
        pass

    assert recebido.get("device") == "cpu", (
        "o vault precisa repassar cfg.embed_device ao construir o encoder, "
        f"e passou {recebido!r}")
