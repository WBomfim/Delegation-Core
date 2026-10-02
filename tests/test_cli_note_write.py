"""`delegation-core note write` — perda de dado silenciosa, e o segundo escritor.

DOIS DEFEITOS, achados na mesma noite, um puxando o outro.

**1. Sobrescrita silenciosa.** `unique_note_path` existe desde a v0.2 e o seu
docstring enumera quem ele protege: "write_note/export_session/maintenance-
classify all derive the filename from just {date}-{safe title}.md - two notes
with the same title on the same day would otherwise silently overwrite the
first note's file *and* its ChromaDB index row, permanently losing its
content."

`cmd_note_write` e um QUARTO caminho que monta exatamente esse nome e nunca
chamou a funcao. Reproduzido em vault temporario em 03/09/2026: duas escritas
com o mesmo titulo no mesmo dia deixaram UM arquivo, com o conteudo da
primeira nota apagado, e o comando imprimiu o mesmo `✓ Scratch/2026-09-03-
Relatorio semanal.md` nas duas vezes. Perda de dado com mensagem de sucesso.

**2. Segundo escritor no ChromaDB.** O docstring de `_delegate` afirma "Every
command that writes to ChromaDB goes through here". Tinha tres chamadores -
reindex, maintain, ingest - e nenhum era o `note`. Medido com o daemon no ar,
amostrando lsof no sqlite a cada segundo enquanto `note write` rodava::

    t+1s a t+6s   sqlite aberto so pelo daemon (pid 449971)
    t+7s          aberto pelo daemon E pelo CLI (pid 477205)

Dois escritores no mesmo SQLite, que e a condicao que corrompeu o FTS deste
vault em 23/08 e custou uma reconstrucao do indice do zero. O proprio daemon
registrou no log: "Index changed on disk by another process - reopening". E
`allow_local_index_fallback: false` estava ligado na config da maquina e nao
impediu, porque a guarda protege o caminho do reindex e nao o do note.

O relato veio de outra maquina, em print de sessao; a conferencia e a medicao
foram feitas aqui.
"""
from __future__ import annotations

import types

import pytest

import delegation_core.cli as cli
import delegation_core.config as config_mod
from delegation_core.config import Config


class VaultFalso:
    """Sem BGE e sem ChromaDB. Registra o que teria sido indexado."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.indexado: list[str] = []

    def index_note(self, content, metadata, doc_id: str = ""):
        self.indexado.append(metadata["path"])
        return True


    def stamp_indexed(self, rel_paths):
        # Acompanha a interface real: o VaultManager carimba o indice
        # incremental depois de indexar com sucesso. Um duble sem este metodo
        # deixaria verde um caminho que producao executa.
        self.carimbadas = getattr(self, "carimbadas", []) + list(rel_paths)
        return len(rel_paths)
    def search(self, *a, **k):
        return []

    def update_note(self, name, content):
        alvos = list((self.cfg.vault).rglob(f"*{name}*.md"))
        if not alvos:
            return {"error": f"Not found: {name}"}
        alvos[0].write_text(alvos[0].read_text(encoding="utf-8") + content, encoding="utf-8")
        return {"path": str(alvos[0].relative_to(self.cfg.vault)), "appended_chars": len(content)}

    def find_notes_by_stem(self, name):
        return list((self.cfg.vault).rglob(f"*{name}*.md"))


@pytest.fixture
def vault(tmp_path, monkeypatch):
    (tmp_path / "Scratch").mkdir()
    cfg = Config(vault_path=str(tmp_path), vault_folders=["Scratch"], engine_mode="agent")
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "cfgdir")
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "cfgdir" / "config.json")
    monkeypatch.setattr(config_mod.Config, "load", staticmethod(lambda: cfg))

    import delegation_core.vault as vault_mod
    criados: list[VaultFalso] = []

    def _fabrica(c):
        v = VaultFalso(c)
        criados.append(v)
        return v

    monkeypatch.setattr(vault_mod, "VaultManager", _fabrica)
    cfg.instancias_de_vault = criados
    return cfg


def _escreve(tmp_path, titulo, texto, **flags):
    entrada = tmp_path / "entrada.md"
    entrada.write_text(texto, encoding="utf-8")
    args = types.SimpleNamespace(folder="Scratch", title=titulo, file=str(entrada),
                                 local=True, timeout=None)
    for k, v in flags.items():
        setattr(args, k, v)
    cli.cmd_note_write(args)


# ── 1. a perda de dado ──────────────────────────────────────────────────────


def test_duas_notas_com_o_mesmo_titulo_no_mesmo_dia_nao_se_apagam(vault, tmp_path):
    """O caso reproduzido. Antes desta correcao sobrava UM arquivo."""
    _escreve(tmp_path, "Relatorio semanal", "PRIMEIRA: escrita na segunda-feira")
    _escreve(tmp_path, "Relatorio semanal", "SEGUNDA: outro assunto, mesmo titulo")

    arquivos = sorted(p.name for p in (tmp_path / "Scratch").glob("*.md"))

    assert len(arquivos) == 2, f"a primeira nota foi destruida: {arquivos}"
    textos = "\n".join((tmp_path / "Scratch" / a).read_text(encoding="utf-8")
                       for a in arquivos)
    assert "PRIMEIRA: escrita na segunda-feira" in textos
    assert "SEGUNDA: outro assunto, mesmo titulo" in textos


def test_a_terceira_colisao_tambem_e_desambiguada(vault, tmp_path):
    for n in range(3):
        _escreve(tmp_path, "Mesmo titulo", f"nota numero {n}")
    assert len(list((tmp_path / "Scratch").glob("*.md"))) == 3


def test_cada_nota_e_indexada_pelo_caminho_que_realmente_ficou_no_disco(vault, tmp_path):
    """A desambiguacao tem que alcancar o INDICE, e nao so o arquivo.

    Escrever em `...-Colide-2.md` e indexar como `...-Colide.md` daria o mesmo
    estrago por outra porta: a segunda nota sobrescreveria a linha da primeira e
    as duas ficariam com o indice apontando para um arquivo que nao e o delas.
    A primeira versao deste teste comparava um conjunto com ele mesmo e nao
    provava nada; esta le o que o vault REGISTROU como indexado.
    """
    _escreve(tmp_path, "Colide", "a")
    _escreve(tmp_path, "Colide", "b")

    indexados = [rel for v in vault.instancias_de_vault for rel in v.indexado]
    no_disco = {f"Scratch/{p.name}" for p in (tmp_path / "Scratch").glob("*.md")}

    assert len(indexados) == 2
    assert len(set(indexados)) == 2, f"as duas notas foram indexadas no mesmo id: {indexados}"
    assert set(indexados) == no_disco
    for rel in indexados:
        assert (tmp_path / rel).is_file()


def test_titulos_diferentes_seguem_em_arquivos_diferentes(vault, tmp_path):
    _escreve(tmp_path, "Um titulo", "a")
    _escreve(tmp_path, "Outro titulo", "b")
    assert len(list((tmp_path / "Scratch").glob("*.md"))) == 2


# ── 2. o roteamento pelo daemon ─────────────────────────────────────────────


@pytest.fixture
def daemon_no_ar(monkeypatch):
    """Daemon respondendo, sem rede: registra as chamadas que chegariam nele."""
    from delegation_core import daemon as daemon_mod

    chamadas = []

    def fake_submit_and_wait(cfg, tool, arguments=None, **kw):
        chamadas.append((tool, arguments))
        # submit_and_wait devolve o payload da ferramenta direto quando ela
        # responde sincronamente (sem job_id), que e o caso de write_note.
        return {"status": "ok", "path": "Scratch/x.md", "folder": "Scratch",
                "appended_chars": 10}

    monkeypatch.setattr(daemon_mod, "is_listening", lambda cfg: True)
    monkeypatch.setattr(daemon_mod, "submit_and_wait", fake_submit_and_wait)
    return chamadas


def test_note_write_entrega_ao_daemon_em_vez_de_abrir_o_indice(vault, tmp_path, daemon_no_ar):
    """O defeito principal: este processo nao pode ser o segundo escritor."""
    _escreve(tmp_path, "Uma nota", "conteudo", local=False)

    assert [t for t, _ in daemon_no_ar] == ["write_note"], (
        "note write tem que passar pelo daemon como reindex/maintain/ingest ja passam"
    )


def test_o_conteudo_e_o_destino_chegam_inteiros_no_daemon(vault, tmp_path, daemon_no_ar):
    _escreve(tmp_path, "Uma nota", "corpo exato da nota", local=False)

    _, argumentos = daemon_no_ar[0]
    assert argumentos["folder"] == "Scratch"
    assert argumentos["title"] == "Uma nota"
    assert "corpo exato da nota" in argumentos["content"]


def test_local_continua_sendo_a_saida_explicita(vault, tmp_path, daemon_no_ar):
    """--local existe para quem quer o trabalho no processo que esta olhando.
    Cair no local em SILENCIO e que seria o segundo escritor sem documentacao."""
    _escreve(tmp_path, "Uma nota", "conteudo", local=True)

    assert daemon_no_ar == []
    assert list((tmp_path / "Scratch").glob("*.md"))


def test_note_update_tambem_passa_pelo_daemon(vault, tmp_path, daemon_no_ar):
    """update reindexa a nota inteira e ainda embute para os links relacionados;
    e o mesmo segundo escritor por outra porta."""
    entrada = tmp_path / "add.md"
    entrada.write_text("mais texto", encoding="utf-8")
    args = types.SimpleNamespace(name="alguma", file=str(entrada), local=False, timeout=None)

    cli.cmd_note_update(args)

    assert [t for t, _ in daemon_no_ar] == ["vault_update_note"]


# ── a bandeira tem que existir para o usuario ───────────────────────────────


@pytest.mark.parametrize("comando", ["write", "update"])
def test_o_parser_oferece_local_nos_dois_comandos_que_escrevem_no_indice(comando):
    """Sem a bandeira registrada, `--local` responde "unrecognized arguments" e
    a saida de emergencia documentada em `_add_local_flag` nao existe.

    Roda o entry point de verdade num subprocesso, como test_cli_commands.py: o
    parser e montado inline em main(), entao nao ha fabrica para chamar, e o que
    interessa aqui e o que um usuario realmente digita.
    """
    import subprocess
    import sys

    r = subprocess.run(
        [sys.executable, "-c", "from delegation_core.cli import main; main()",
         "note", comando, "--help"],
        capture_output=True, text=True, timeout=120,
    )

    assert "--local" in r.stdout, (
        f"`delegation-core note {comando}` escreve no indice e nao oferece --local"
    )


# ── o carimbo que o roteamento nao pode trocar ──────────────────────────────


def test_a_nota_escrita_por_uma_pessoa_nao_e_carimbada_como_gerada_por_IA(vault, tmp_path):
    """`compose_note` fixava ai_generated: true, e o CLI escrevia false a mao.

    Rotear pelo daemon sem tratar isso trocaria o carimbo de toda nota que uma
    pessoa digitou no terminal. O campo nao e lido por nenhuma decisao do
    codigo, mas gravar nele o que nao aconteceu e escrever coisa falsa no vault
    do usuario, e a correcao de um defeito nao autoriza introduzir outro.
    """
    _escreve(tmp_path, "Escrita a mao", "texto que uma pessoa digitou")

    nota = next((tmp_path / "Scratch").glob("*.md")).read_text(encoding="utf-8")

    assert "ai_generated: false" in nota
    assert "ai_generated: true" not in nota


def test_o_daemon_recebe_o_carimbo_explicito_e_nao_o_padrao(vault, tmp_path, daemon_no_ar):
    """O padrao da ferramenta MCP e true, que e certo para um agente. O CLI tem
    que dizer false em voz alta, senao o roteamento muda o dado em silencio."""
    _escreve(tmp_path, "Escrita a mao", "texto", local=False)

    _, argumentos = daemon_no_ar[0]
    assert argumentos["ai_generated"] is False


def test_a_ferramenta_mcp_continua_carimbando_true_por_padrao():
    """Quem nao passa nada e um agente, e um agente gerou a nota."""
    from delegation_core.notes import compose_note

    assert "ai_generated: true" in compose_note("T", "corpo", "2026-09-04")
