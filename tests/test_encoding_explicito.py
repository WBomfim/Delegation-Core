"""Nenhuma leitura ou escrita de TEXTO sem `encoding=`.

Sem ele o Python usa `locale.getpreferredencoding()`: UTF-8 nesta maquina,
cp1252 no Windows, e ANSI_X3.4-1968 (ASCII puro) em qualquer processo com
LC_ALL=C, que e o que um servidor enxuto e boa parte dos ambientes de CI tem.

MEDIDO em 03/09/2026, com o interpretador rodando sob LC_ALL=C:

    write_text(unit)                    -> UnicodeEncodeError em 'ã'
    write_text(unit, encoding="utf-8")  -> ok

    ler um .groovy UTF-8 com read_text(errors="replace"):
      classe `ValidaçãoSpec` -> Valida\\ufffd\\ufffd\\ufffd\\ufffdoSpec
      com encoding="utf-8"   -> ValidaçãoSpec
      e NENHUM erro, porque `errors="replace"` engole a falha

Os dois lugares que faltavam eram vizinhos de lugares que ja faziam certo:
`service.py` escreve a unit systemd DELE com encoding="utf-8" e o `wizard.py`
escrevia a dele sem; e dezenas de `read_text(encoding=...)` no projeto contra
dois em `graph/extract.py` sem.

Por isso este arquivo proibe a FORMA em vez de corrigir as instancias, que e o
mesmo idioma de `test_version_consistency` e `test_docs_not_stale`: sincronizar
copia nao resolve deriva, eliminar a copia resolve. Um `read_text()` novo sem
encoding falha aqui em vez de virar mojibake no vault de alguem.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

RAIZ = pathlib.Path(__file__).resolve().parent.parent

#: Chamadas que se PARECEM com IO de texto e nao aceitam `encoding`.
#: Cada entrada e um nome de funcao, nao um arquivo: a isencao vale pela API
#: chamada e nao pelo lugar, senao ela vira um lugar onde o defeito pode voltar.
_SEM_ENCODING_POR_NATUREZA = {
    "tarfile.open",     # arquivo compactado
    "zipfile.open",     # idem
    "os.open",          # devolve fd cru, sem camada de texto
    "opener.open",      # urllib
    "z.open", "zf.open",
    "Image.open",       # PIL: le a imagem em binario, nao ha camada de texto
}


def _chamadas_de_texto_sem_encoding() -> list[tuple[str, int, str]]:
    faltando = []
    alvos = list((RAIZ / "src").rglob("*.py")) + list((RAIZ / "hooks").rglob("*.py"))
    for p in sorted(alvos):
        texto = p.read_text(encoding="utf-8")
        try:
            arvore = ast.parse(texto)
        except SyntaxError:
            continue
        linhas = texto.splitlines()
        for no in ast.walk(arvore):
            if not isinstance(no, ast.Call):
                continue
            nome = ""
            if isinstance(no.func, ast.Attribute):
                dono = getattr(no.func.value, "id", "")
                nome = f"{dono}.{no.func.attr}" if dono else no.func.attr
            elif isinstance(no.func, ast.Name):
                nome = no.func.id
            base = nome.rsplit(".", 1)[-1]
            if base not in ("read_text", "write_text", "open"):
                continue
            if nome in _SEM_ENCODING_POR_NATUREZA:
                continue
            if any(k.arg == "encoding" for k in no.keywords):
                continue
            # open(..., "rb") e afins nao tem camada de texto
            modos = [a.value for a in no.args
                     if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if any("b" in m for m in modos if len(m) <= 3):
                continue
            faltando.append((str(p.relative_to(RAIZ)), no.lineno,
                             linhas[no.lineno - 1].strip()[:90]))
    return faltando


def test_nenhuma_chamada_de_texto_sem_encoding():
    faltando = _chamadas_de_texto_sem_encoding()
    assert not faltando, (
        "IO de texto sem encoding explicito, que decodifica pelo locale:\n  "
        + "\n  ".join(f"{a}:{n}  {t}" for a, n, t in faltando)
    )


def test_a_varredura_realmente_enxerga_alguma_coisa():
    """Uma varredura que nao acha nada porque esta quebrada passa igual a uma
    que nao acha nada porque esta tudo certo. Esta prova que ela enxerga."""
    fonte = ast.parse('p.read_text()\np.write_text(x)\nopen("f")\n')
    achou = [n for n in ast.walk(fonte)
             if isinstance(n, ast.Call) and not any(k.arg == "encoding" for k in n.keywords)]
    assert len(achou) == 3


@pytest.mark.parametrize("chamada", [
    'path.read_text(errors="replace")',
    "arquivo.write_text(conteudo)",
])
def test_a_varredura_pegaria_estas_formas(chamada, tmp_path):
    """As duas formas exatas que estavam no codigo antes desta correcao."""
    alvo = tmp_path / "src" / "delegation_core"
    alvo.mkdir(parents=True)
    (alvo / "m.py").write_text(f"def f(path, arquivo, conteudo):\n    {chamada}\n",
                               encoding="utf-8")

    arvore = ast.parse((alvo / "m.py").read_text(encoding="utf-8"))
    chamadas = [n for n in ast.walk(arvore) if isinstance(n, ast.Call)]

    assert chamadas and not any(k.arg == "encoding" for k in chamadas[0].keywords)


# ── a premissa da guarda, medida e nao afirmada ─────────────────────────────


@pytest.mark.skipif(not hasattr(__import__("os"), "setsid"),
                    reason="depende de locale POSIX")
def test_sob_locale_ascii_a_diferenca_e_real(tmp_path):
    """Prova que a exigencia acima nao e superstição.

    Roda um interpretador com LC_ALL=C e compara as duas leituras do MESMO
    arquivo UTF-8. Este e o experimento que encontrou os dois `read_text` do
    `graph/extract.py`: a versao sem encoding devolve U+FFFD sem levantar nada,
    porque `errors="replace"` engole a falha, e o no do grafo nasce com o nome
    corrompido.
    """
    import subprocess
    import sys

    fonte = tmp_path / "Spec.groovy"
    fonte.write_bytes("class ValidaXcaoSpec {}\n".replace("Xcao", "ção").encode("utf-8"))

    programa = (
        "import sys,re,locale\n"
        "p=sys.argv[1]\n"
        "sem=open(p, errors='replace').read()\n"
        "com=open(p, encoding='utf-8', errors='replace').read()\n"
        "n_sem=re.search(r'class\\s+(\\S+)', sem).group(1)\n"
        "n_com=re.search(r'class\\s+(\\S+)', com).group(1)\n"
        "print(locale.getpreferredencoding(False))\n"
        "print('IGUAIS' if n_sem==n_com else 'DIFERENTES')\n"
        "print('FFFD' if '\\ufffd' in n_sem else 'LIMPO')\n"
    )
    amb = {"LC_ALL": "C", "PATH": "/usr/bin:/bin",
           "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0"}
    r = subprocess.run([sys.executable, "-c", programa, str(fonte)],
                       capture_output=True, text=True, env=amb, timeout=60)

    codificacao, iguais, sujeira = r.stdout.split()
    if "UTF-8" in codificacao.upper():
        pytest.skip(f"este interpretador ignora o locale ({codificacao})")

    assert iguais == "DIFERENTES", "sem encoding o nome da classe muda"
    assert sujeira == "FFFD", "e muda para caractere de substituicao, sem erro"
