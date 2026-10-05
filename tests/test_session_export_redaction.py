"""O hook grava o transcript em texto puro e depois o indexa. Segredo nao entra.

Um token da ClickUp foi colado numa sessao em 02/09/2026. Este hook teria
gravado a conversa inteira em `Sessions/`, e o vault indexa o que grava: o token
ficaria legivel, pesquisavel e recuperavel por toda sessao futura.

A redacao foi escrita na copia INSTALADA naquele mesmo dia e nunca voltou para o
repositorio. Descoberto em 03/09 ao comparar as duas antes de publicar: o repo
tinha 313 linhas e a copia instalada 351, e as 38 de diferenca eram exatamente
isto. Toda instalacao nova continuava recebendo a versao sem redacao.

Os segredos aqui sao sinteticos, com a forma certa e sem valor nenhum. Nenhum
deles e, nem foi, uma credencial de verdade.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
HOOK = RAIZ / "src" / "delegation_core" / "hooks" / "session_export.py"


@pytest.fixture(scope="module")
def hook():
    """Carrega o hook como modulo. Ele e stdlib-only e nao importa o pacote."""
    if not HOOK.is_file():
        pytest.skip("hooks/session_export.py nao esta neste checkout")
    spec = importlib.util.spec_from_file_location("_session_export_sob_teste", HOOK)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── cada forma que precisa sumir ────────────────────────────────────────────

CASOS = [
    ("clickup",   "pk_12345678_ABCDEFGHIJKLMNOPQRSTUVWX", "TOKEN CLICKUP"),
    ("anthropic", "sk-ant-api03-" + "A" * 40,             "CHAVE ANTHROPIC"),
    ("openai",    "sk-" + "b" * 40,                       "CHAVE OPENAI"),
    ("github",    "ghp_" + "c" * 36,                      "TOKEN GITHUB"),
    ("slack",     "xoxb-1234567890-abcdefghij",           "TOKEN SLACK"),
    ("aws",       "AKIAIOSFODNN7EXAMPLE",                 "CHAVE AWS"),
    ("jwt",       "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27u",
     "JWT"),
]


@pytest.mark.parametrize("rotulo,segredo,marca", CASOS, ids=[c[0] for c in CASOS])
def test_o_segredo_nao_sobrevive(hook, rotulo, segredo, marca):
    texto, n = hook._redigir(f"o usuario colou {segredo} no meio da conversa")
    assert segredo not in texto, f"{rotulo} passou intacto"
    assert marca in texto
    assert n == 1


def test_chave_privada_multilinha(hook):
    """`re.S` importa aqui: sem ele o `.` nao cruza as quebras de linha e a
    chave inteira passa, porque o BEGIN e o END estao em linhas diferentes."""
    chave = ("-----BEGIN RSA PRIVATE KEY-----\n"
             "MIIEowIBAAKCAQEAx7Xm9vQ\n2fL8pQ==\n"
             "-----END RSA PRIVATE KEY-----")
    texto, n = hook._redigir(f"segue a chave:\n{chave}\nfim")
    assert "BEGIN RSA PRIVATE KEY" not in texto
    assert "MIIEowIBAAKCAQEAx7Xm9vQ" not in texto
    assert n == 1


def test_varios_segredos_de_uma_vez(hook):
    bruto = "\n".join(s for _, s, _ in CASOS)
    texto, n = hook._redigir(bruto)
    assert n == len(CASOS)
    for _, segredo, _ in CASOS:
        assert segredo not in texto


def test_o_mesmo_segredo_repetido_some_em_todas_as_ocorrencias(hook):
    s = "pk_12345678_ABCDEFGHIJKLMNOPQRSTUVWX"
    texto, n = hook._redigir(f"{s} ... depois de novo {s}")
    assert s not in texto
    assert n == 2


# ── o que NAO pode ser redigido ─────────────────────────────────────────────

@pytest.mark.parametrize("inocente", [
    "sk-curto",
    "AKIAMINUSCULO",
    "pk_sem_numero_ABC",
    "o commit ghp foi revertido",
    "eyJ isolado sem os tres segmentos",
    "uma frase normal sobre tokens e chaves",
])
def test_texto_comum_nao_e_mutilado(hook, inocente):
    """Uma redacao agressiva demais corrompe o transcript, que e a coisa que o
    hook existe para preservar."""
    texto, n = hook._redigir(inocente)
    assert texto == inocente
    assert n == 0


def test_texto_sem_segredo_algum_e_devolvido_identico(hook):
    original = "## Sessao\n\nUm dia normal de trabalho.\n"
    texto, n = hook._redigir(original)
    assert texto == original and n == 0


# ── a integracao: o hook aplica isto no que escreve ─────────────────────────

def test_o_hook_redige_ANTES_de_escrever_o_tmp(hook):
    """O `.tmp` fica no mesmo disco. Redigir depois de escrever deixaria o
    segredo em claro pelo tempo que o arquivo existisse."""
    fonte = HOOK.read_text(encoding="utf-8")
    pos_redacao = fonte.index("_redigir(content)")
    pos_escrita = fonte.index('tmp.write_text(content')
    assert pos_redacao < pos_escrita, "redige depois de escrever o .tmp"


def _rodar_o_hook(hook, monkeypatch, tmp_path, texto_do_usuario):
    """Roda `main()` de ponta a ponta contra um vault temporario.

    Uma asercao sobre o texto-fonte nao e evidencia de que o arquivo escrito
    esta limpo: a primeira versao destes testes so olhava a fonte, e uma
    mutacao que descartava a contagem passou por todos eles.
    """
    import io
    import json as _json

    vault = tmp_path / "vault"
    (vault / "Sessions").mkdir(parents=True)

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(_json.dumps({
        "type": "user",
        "timestamp": "2026-09-03T10:00:00Z",
        "message": {"role": "user", "content": texto_do_usuario},
    }) + "\n", encoding="utf-8")

    monkeypatch.setattr(hook, "_load_config", lambda: {"vault_path": str(vault)})
    monkeypatch.setattr(hook, "_trigger_reindex", lambda: False)
    monkeypatch.setattr(hook.sys, "stdin", io.StringIO(_json.dumps({
        "transcript_path": str(transcript),
        "session_id": "abcdef1234567890",
        "cwd": str(tmp_path),
    })))

    try:
        hook.main()
    except SystemExit:
        pass

    escritos = list((vault / "Sessions").glob("*.md"))
    assert escritos, "o hook nao escreveu nota nenhuma"
    return escritos[0].read_text(encoding="utf-8")


def test_o_segredo_nao_chega_ao_arquivo_escrito(hook, monkeypatch, tmp_path):
    """A prova que importa: o arquivo no disco, nao a fonte do hook."""
    segredo = "pk_12345678_ABCDEFGHIJKLMNOPQRSTUVWX"
    nota = _rodar_o_hook(hook, monkeypatch, tmp_path,
                         f"meu token da clickup e {segredo}, guarda ai")

    assert segredo not in nota
    assert "TOKEN CLICKUP REMOVIDO" in nota


def test_a_nota_escrita_diz_quantos_segredos_sairam(hook, monkeypatch, tmp_path):
    """Redacao silenciosa nao ensina nada a ninguem: um transcript que teve um
    token e um transcript cujo token precisa ser rotacionado, e a pessoa so
    consegue agir se a nota disser."""
    nota = _rodar_o_hook(
        hook, monkeypatch, tmp_path,
        "pk_12345678_ABCDEFGHIJKLMNOPQRSTUVWX e tambem AKIAIOSFODNN7EXAMPLE")
    assert "segredos_removidos: 2" in nota


def test_sem_segredo_a_nota_nao_ganha_o_campo(hook, monkeypatch, tmp_path):
    """Um `segredos_removidos: 0` em toda nota treinaria a pessoa a ignorar o
    campo, que e o unico sinal de que algo precisa ser rotacionado."""
    nota = _rodar_o_hook(hook, monkeypatch, tmp_path, "um dia normal de trabalho")
    assert "segredos_removidos" not in nota
    assert "type: session-transcript" in nota


def test_a_ancora_do_frontmatter_existe_de_verdade(hook):
    """O contador e injetado depois de `type: session-transcript`. Se o
    formatador deixar de emitir essa linha, a contagem some sem erro nenhum e
    ninguem descobre que houve redacao."""
    fonte = HOOK.read_text(encoding="utf-8")
    assert fonte.count('"type: session-transcript"') >= 2, (
        "a linha que ancora `segredos_removidos` nao e mais emitida pelo formatador"
    )


def test_o_repo_nao_pode_ficar_atras_da_copia_instalada(hook):
    """A regressao que este arquivo existe para impedir.

    A correcao viveu 24 horas so na copia instalada desta maquina. Enquanto
    estivesse so la, toda instalacao nova recebia o hook sem redacao.
    """
    fonte = HOOK.read_text(encoding="utf-8")
    assert "_SEGREDOS" in fonte and "def _redigir" in fonte
