"""
repair.py — encontra e conserta as notas que foram sintetizadas a partir de nada.

O piso de substancia em `extractor.py` impede que aconteca de novo. Ele nao
desfaz o que ja aconteceu, e o que ja aconteceu sao notas com resumo, topicos,
pessoas e decisoes, todas invencao, indistinguiveis de registro de verdade para
quem le e para a busca semantica.

**Como uma nota assim e reconhecida depois do fato.** Ela propria nao carrega
marca nenhuma: foi escrita pelo mesmo caminho de qualquer outra e ate o
`quality_score` saiu 1.0. A evidencia esta no arquivo de origem, que o pipeline
preserva em `_processed/`. Reextrair a origem com o piso de hoje responde a
pergunta que ninguem podia fazer na epoca: havia texto suficiente ali para
justificar esta nota?

Por isso a busca parte das origens e nao das notas. E tambem por isso ela e
honesta sobre o que nao alcanca: uma origem que foi apagada de `_processed/`
deixa a nota orfa de evidencia, e este modulo prefere nao listar a chutar.

Nada aqui apaga nota. O padrao e substituir o corpo pelo toco, preservando o
frontmatter: o material continua achavel pelo nome e passa a dizer o que e, em
vez de afirmar coisas que ninguem disse. Arquivar e a alternativa, para quem
prefere tirar da busca.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path

from .extractor import SUPPORTED, extract, is_no_text_stub
from .vault import safe_filename

logger = logging.getLogger("repair")

#: Onde as notas arquivadas vao parar, quando se escolhe arquivar em vez de
#: substituir. Mesmo destino que o vault ja usa para material retirado de
#: circulacao sem ser destruido.
PASTA_DE_ARQUIVO = "_dump/_archive"

#: Marca no frontmatter da nota consertada. Existe para que uma segunda passada
#: nao reprocesse o que ja foi tratado, e para que quem abrir a nota saiba que
#: ela mudou e por que.
MARCA = "synthesised_from_empty_source"


def _origens(vault: Path) -> list[Path]:
    """Os arquivos que o pipeline consumiu e guardou."""
    processados = vault / "_processed"
    if not processados.is_dir():
        return []
    return sorted(
        f for f in processados.rglob("*")
        if f.is_file() and f.suffix.lower() in SUPPORTED
    )


def _normalizar(nome: str) -> str:
    """Reduz um nome ao que sobrevive a qualquer variacao de separador.

    `safe_filename` preserva espaco, mas nem todo caminho que ja escreveu nota
    neste vault preservou: existem notas com hifen onde a origem tem espaco, e
    com underscore onde tem hifen. Comparar os nomes crus falha exatamente
    nesses casos, e sao eles que precisam ser achados.
    """
    import re
    return re.sub(r"[\s_-]+", "-", nome.strip().lower()).strip("-")


def _notas_da_origem(vault: Path, origem: Path) -> list[Path]:
    """As notas que saíram deste arquivo.

    O organizer nomeia a nota como `{data}-{safe_filename(stem)}.md` e registra
    o stem original como alias, entao o stem e o elo. Comparado normalizado,
    porque o separador varia entre os caminhos que ja escreveram aqui.
    """
    alvo = _normalizar(safe_filename(origem.stem))
    if not alvo:
        return []
    achadas = []
    for nota in vault.rglob("*.md"):
        partes = nota.relative_to(vault).parts
        if partes and partes[0].startswith("_"):
            continue                      # _inbox, _processed, _dump, _failed
        if alvo in _normalizar(nota.stem):
            achadas.append(nota)
    return sorted(achadas)


def _ja_tratada(nota: Path) -> bool:
    """A nota ja carrega a marca do toco, ou nao pode ser lida?

    As duas respostas sao True, e a segunda e o ponto. `False` significa "ainda
    precisa ser reescrita", e devolver isso para uma nota que nem abriu manda o
    `aplicar()` passar por cima de um arquivo cujo conteudo ninguem viu.

    MEDIDO com uma nota sem permissao de leitura, antes desta mudanca: devolvia
    False. E a direcao errada da regra que `_unindexed_notes` carrega no proprio
    docstring, "degrade to 'cannot tell' rather than to 'all fine'": aqui o
    "tudo certo" e destrutivo, entao quem nao pode ser lido nao pode ser
    reescrito.
    """
    try:
        cabeca = nota.read_text(encoding="utf-8")[:800]
    except OSError as e:
        logger.warning("nao consegui ler %s (%s) - tratando como ja tratada para "
                       "nao reescrever no escuro", nota, e)
        return True
    return MARCA in cabeca


def encontrar(cfg) -> dict:
    """Origens sem texto suficiente, e as notas que sairam delas.

    Nao muda nada. E o passo que responde "quanto disto existe aqui" antes de
    qualquer decisao sobre o que fazer.
    """
    vault = Path(cfg.vault)
    achados, sem_nota, ilegiveis = [], [], []

    for origem in _origens(vault):
        try:
            texto = extract(origem)
        except Exception as e:                       # noqa: BLE001
            ilegiveis.append({"source": origem.name, "error": str(e)[:120]})
            continue
        if not is_no_text_stub(texto):
            continue

        notas = [n for n in _notas_da_origem(vault, origem) if not _ja_tratada(n)]
        if not notas:
            sem_nota.append(origem.name)
            continue
        achados.append({
            "source": origem.relative_to(vault).as_posix(),
            "stub": texto,
            "notes": [n.relative_to(vault).as_posix() for n in notas],
        })

    return {
        "found": achados,
        "notes_total": sum(len(a["notes"]) for a in achados),
        "sources_without_note": sem_nota,
        "unreadable": ilegiveis,
    }


def _troca_o_corpo(nota: Path, toco: str) -> None:
    """Substitui o corpo pelo toco, preservando o frontmatter que existir."""
    texto = nota.read_text(encoding="utf-8")
    fim_fm = -1
    if texto.startswith("---\n"):
        fim_fm = texto.find("\n---\n", 4)

    aviso = (
        f"{MARCA}: true\n"
        f"{MARCA}_at: {datetime.now().isoformat(timespec='seconds')}\n"
    )
    corpo = (
        "## Sem texto na origem\n\n"
        "O conteudo anterior desta nota foi sintetizado a partir de um arquivo "
        "sem texto suficiente para resumir, entao nao descrevia nada que "
        "estivesse no documento. Foi substituido pelo registro do arquivo:\n\n"
        "```\n" + toco + "\n```\n"
    )

    if fim_fm != -1:
        fm = texto[4:fim_fm]
        nota.write_text(f"---\n{fm}\n{aviso}---\n\n{corpo}", encoding="utf-8")
    else:
        nota.write_text(f"---\n{aviso}---\n\n{corpo}", encoding="utf-8")


def aplicar(cfg, *, arquivar: bool = False) -> dict:
    """Conserta o que `encontrar` achou.

    `arquivar=False`, o padrao, troca o corpo da nota pelo toco. O nome do
    arquivo continua o mesmo, a nota continua no lugar, e a busca continua
    encontrando o material, so que agora dizendo o que ele e.

    `arquivar=True` move a nota para `_dump/_archive/`, tirando-a da busca. E a
    escolha de quem prefere que aquilo simplesmente nao apareca.
    """
    vault = Path(cfg.vault)
    relatorio = encontrar(cfg)
    tratadas, falhas = [], []

    destino = vault / PASTA_DE_ARQUIVO
    if arquivar:
        destino.mkdir(parents=True, exist_ok=True)

    for achado in relatorio["found"]:
        for rel in achado["notes"]:
            nota = vault / rel
            try:
                if arquivar:
                    alvo = destino / nota.name
                    n = 1
                    while alvo.exists():
                        alvo = destino / f"{nota.stem}-{n}{nota.suffix}"
                        n += 1
                    shutil.move(str(nota), str(alvo))
                    tratadas.append({"note": rel, "action": "archived",
                                     "to": alvo.relative_to(vault).as_posix()})
                else:
                    _troca_o_corpo(nota, achado["stub"])
                    tratadas.append({"note": rel, "action": "replaced_with_stub"})
            except OSError as e:
                falhas.append({"note": rel, "error": str(e)})

    relatorio["applied"] = tratadas
    relatorio["failures"] = falhas
    relatorio["status"] = "ok" if not falhas else "partial"
    return relatorio
