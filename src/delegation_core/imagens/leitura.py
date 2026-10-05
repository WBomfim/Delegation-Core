"""Imagem vira texto buscavel: EXIF, data da captura de tela, e OCR com tesseract.

## Por que existe

O AGENT_GUIDE dizia: "Images are not supported". Nesta maquina ha 81 capturas
de tela em ~/Pictures/Screenshots, varias de erro vindo de outra maquina (o
Jordan manda print do que acontece la). Nenhuma era buscavel. O fototriagem
lida com metadado de foto (EXIF, data do sidecar do Takeout); aqui o mesmo
tipo de leitura vira nota.

Medido em 27/09 numa captura real em portugues: o tesseract com os dados
"fast" de por+eng devolveu o texto quase literal em 0,25 s.

## O que sai

Um markdown com titulo, data (EXIF DateTimeOriginal, depois o padrao do nome
da captura, depois o mtime), dimensoes, camera se houver, e o texto do OCR. Sem
tesseract ou sem os idiomas, a nota sai so com o metadado e diz que o OCR nao
rodou, em vez de falhar: metadado sozinho ja acha a imagem por data e nome.

## Criterio de lego

PIL e o binario do tesseract, chamado por subprocess. Nao sabe o que e vault.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

EXTENSOES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"})
IDIOMAS_PADRAO = ("por", "eng")

#: Icone e logotipo nao tem texto que valha uma linha no indice, e ingerir um
#: repositorio de codigo passaria o OCR em milhares deles (o grafana tem
#: milhares de icones). Abaixo disto, nem roda o OCR.
MIN_LADO = 120
#: OCR que acha letras soltas numa foto e ruido, nao texto. Contar caracteres
#: foi a primeira regra e reprovou "ERRO 422 no onboarding" (19), que e
#: justamente o texto curto que importa numa captura de erro. Conta palavra:
#: pelo menos duas com tres ou mais letras ou digitos.
MIN_PALAVRAS = 2
_PALAVRA = re.compile(r"[^\W_]{3,}", re.UNICODE)
SEM_TEXTO = "_Sem texto:"

_NOME_CAPTURA = [
    re.compile(r"(\d{4})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})"),                 # Screenshot_20260923_225121
    re.compile(r"(\d{4})-(\d{2})-(\d{2})[ _T](\d{2})[-.:h](\d{2})[-.:m](\d{2})"),    # Screenshot from 2026-09-23 22-51-21
]


# WhatsApp e captura do macOS: "2026-09-30 at 10.48.55 AM", hora de 12h.
_NOME_12H = re.compile(r"(\d{4})-(\d{2})-(\d{2}) at (\d{1,2})\.(\d{2})\.(\d{2})\s?(AM|PM)", re.IGNORECASE)


def data_da_captura(nome: str) -> str | None:
    m = _NOME_12H.search(nome)
    if m:
        a, mes, d, h, mi, s, ampm = m.groups()
        h = int(h) % 12 + (12 if ampm.upper() == "PM" else 0)
        try:
            return datetime(int(a), int(mes), int(d), h, int(mi), int(s)).isoformat(timespec="minutes")
        except ValueError:
            return None
    for rx in _NOME_CAPTURA:
        m = rx.search(nome)
        if m:
            try:
                return datetime(*map(int, m.groups())).isoformat(timespec="minutes")
            except ValueError:
                continue
    return None


def _exif(caminho: Path) -> dict:
    try:
        from PIL import Image
    except ImportError:
        return {}
    try:
        with Image.open(caminho) as im:
            info = {"largura": im.width, "altura": im.height, "formato": im.format or ""}
            ex = im.getexif() if hasattr(im, "getexif") else None
            if ex:
                sub = ex.get_ifd(0x8769) if hasattr(ex, "get_ifd") else {}
                bruto = sub.get(36867) or ex.get(306)
                if bruto:
                    try:
                        info["data_exif"] = datetime.strptime(str(bruto).strip(), "%Y:%m:%d %H:%M:%S").isoformat(timespec="minutes")
                    except ValueError:
                        pass
                camera = " ".join(str(ex.get(t, "")).strip() for t in (271, 272)).strip()
                if camera:
                    info["camera"] = camera
                if ex.get(0x8825):
                    info["tem_gps"] = True
            return info
    except Exception:  # noqa: BLE001 - imagem ilegivel vira nota so com o que se sabe
        return {}


def idiomas_disponiveis(tessdata: str | Path | None = None) -> list[str]:
    exe = shutil.which("tesseract")
    if not exe:
        return []
    env = dict(os.environ)
    if tessdata:
        env["TESSDATA_PREFIX"] = str(tessdata)
    try:
        r = subprocess.run([exe, "--list-langs"], capture_output=True, text=True, timeout=10, env=env)
    except (OSError, subprocess.SubprocessError):
        return []
    return [l.strip() for l in (r.stdout + r.stderr).splitlines()[1:] if l.strip() and " " not in l.strip()]


def ocr(caminho: Path, tessdata: str | Path | None = None, idiomas: tuple[str, ...] = IDIOMAS_PADRAO,
        timeout: float = 60) -> tuple[str, str]:
    """(texto, motivo). Texto vazio com motivo quando o OCR nao pode rodar."""
    exe = shutil.which("tesseract")
    if not exe:
        return "", "tesseract nao instalado"
    tem = set(idiomas_disponiveis(tessdata))
    usar = [i for i in idiomas if i in tem]
    if not usar:
        return "", f"dados de idioma ausentes ({'+'.join(idiomas)}); rode `delegation-core ocr-setup`"
    env = dict(os.environ)
    if tessdata:
        env["TESSDATA_PREFIX"] = str(tessdata)
    try:
        r = subprocess.run([exe, str(caminho), "-", "-l", "+".join(usar)],
                           capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return "", f"OCR passou de {timeout:.0f} s"
    except OSError as e:
        return "", f"tesseract falhou: {e}"
    texto = "\n".join(l.rstrip() for l in r.stdout.splitlines())
    texto = re.sub(r"\n{3,}", "\n\n", texto).strip()
    return texto, "" if texto else "OCR nao achou texto"


def ler_imagem(caminho: str | Path, tessdata: str | Path | None = None,
               idiomas: tuple[str, ...] = IDIOMAS_PADRAO) -> dict:
    p = Path(caminho)
    info = _exif(p)
    data = info.get("data_exif") or data_da_captura(p.name) or \
        datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="minutes")
    if info.get("largura") and (info["largura"] < MIN_LADO or info["altura"] < MIN_LADO):
        texto, motivo = "", f"imagem pequena demais para ter texto ({info['largura']} x {info['altura']})"
    else:
        texto, motivo = ocr(p, tessdata, idiomas)
        if texto and len(_PALAVRA.findall(texto)) < MIN_PALAVRAS:
            texto, motivo = "", "OCR achou so fragmentos"
    return {"nome": p.name, "data": data, **info, "texto": texto, "ocr_motivo": motivo}


def markdown_tem_texto(md: str) -> bool:
    """A nota de imagem traz texto lido, ou so metadado? A ingestao so indexa a primeira."""
    return bool(md) and SEM_TEXTO not in md


def para_markdown(info: dict) -> str:
    ln = [f"# {info['nome']}", "", f"- Data: {info['data']}"]
    if info.get("largura"):
        ln.append(f"- Dimensoes: {info['largura']} x {info['altura']} ({info.get('formato', '')})")
    if info.get("camera"):
        ln.append(f"- Camera: {info['camera']}")
    if info.get("tem_gps"):
        ln.append("- Tem localizacao GPS no EXIF")
    ln += ["", "## Texto na imagem (OCR)", ""]
    ln.append(info["texto"] if info.get("texto") else f"{SEM_TEXTO} {info.get('ocr_motivo') or 'OCR vazio'}._")
    return "\n".join(ln) + "\n"
