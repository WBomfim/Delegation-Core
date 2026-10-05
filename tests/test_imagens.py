"""Pacote imagens: data da captura, EXIF, OCR (com e sem tesseract) e a ligacao no extrator."""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

import pytest

from delegation_core import extractor
from delegation_core.imagens import leitura
from delegation_core.imagens import data_da_captura, ler_imagem, para_markdown

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

TESSDATA = os.environ.get("DC_TESSDATA_TESTE", "")


@pytest.mark.parametrize("nome,esperado", [
    ("Screenshot_20260923_225121.png", "2026-09-23T22:51"),
    ("Screenshot from 2026-09-23 22-51-21.png", "2026-09-23T22:51"),
    ("Captura de tela 2026-09-01 18.45.44.png", "2026-09-01T18:45"),
    # WhatsApp e captura do macOS: "at", hora de 12h com AM/PM. As imagens de
    # outra maquina chegam assim, e sem o padrao a data caia para o mtime (02/10/2026).
    ("WhatsApp Image 2026-09-30 at 10.48.55 AM.jpeg", "2026-09-30T10:48"),
    ("WhatsApp Image 2026-10-02 at 12.05.00 PM.jpeg", "2026-10-02T12:05"),
    ("Screenshot 2026-10-02 at 11.22.20 PM.png", "2026-10-02T23:22"),
    ("Screenshot 2026-10-02 at 12.10.04 AM.png", "2026-10-02T00:10"),
    ("Screenshot_20261399_225121.png", None),
    ("foto qualquer.png", None),
])
def test_data_da_captura_pelo_nome(nome, esperado):
    assert data_da_captura(nome) == esperado


def _imagem(tmp_path, nome="x.png", texto="", exif_data=None):
    im = Image.new("RGB", (400, 120), "white")
    if texto:
        d = ImageDraw.Draw(im)
        try:
            fonte = ImageFont.load_default(size=28)
        except TypeError:
            fonte = ImageFont.load_default()
        d.text((10, 40), texto, fill="black", font=fonte)
    p = tmp_path / nome
    if exif_data:
        ex = Image.Exif()
        ex[306] = exif_data
        ex[271] = "Google"
        ex[272] = "Pixel 8"
        im.save(p, exif=ex)
    else:
        im.save(p)
    return p


def test_exif_da_data_e_camera(tmp_path):
    p = _imagem(tmp_path, "foto.jpg", exif_data="2026:08:30 10:15:00")
    i = ler_imagem(p)
    assert i["data"] == "2026-08-30T10:15" and i["camera"] == "Google Pixel 8" and i["largura"] == 400


def test_sem_exif_usa_o_nome_e_depois_o_mtime(tmp_path):
    assert ler_imagem(_imagem(tmp_path, "Screenshot_20260901_184544.png"))["data"] == "2026-09-01T18:45"
    p = _imagem(tmp_path, "sem data.png")
    os.utime(p, (datetime(2026, 7, 5, 12, 0).timestamp(),) * 2)
    assert ler_imagem(p)["data"] == "2026-07-05T12:00"


def test_sem_tesseract_sai_so_o_metadado(tmp_path, monkeypatch):
    monkeypatch.setattr(leitura.shutil, "which", lambda n: None)
    i = ler_imagem(_imagem(tmp_path, texto="ERRO 422"))
    assert i["texto"] == "" and i["ocr_motivo"] == "tesseract nao instalado"
    md = para_markdown(i)
    assert "_Sem texto: tesseract nao instalado._" in md and "400 x 120" in md


def test_sem_idioma_manda_rodar_o_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(leitura, "idiomas_disponiveis", lambda t=None: ["osd"])
    monkeypatch.setattr(leitura.shutil, "which", lambda n: "/usr/bin/tesseract")
    texto, motivo = leitura.ocr(_imagem(tmp_path), None)
    assert texto == "" and "ocr-setup" in motivo


def test_imagem_ilegivel_nao_quebra(tmp_path):
    p = tmp_path / "quebrada.png"
    p.write_bytes(b"nao e png")
    i = ler_imagem(p)
    assert "largura" not in i and i["nome"] == "quebrada.png"


@pytest.mark.skipif(not shutil.which("tesseract") or not TESSDATA,
                    reason="OCR real precisa de tesseract e DC_TESSDATA_TESTE com por/eng")
def test_ocr_real_le_o_texto(tmp_path):
    i = ler_imagem(_imagem(tmp_path, texto="ERRO 422 no onboarding"), tessdata=TESSDATA)
    assert "422" in i["texto"] and "onboarding" in i["texto"].lower()


def test_extrator_aceita_imagem_e_devolve_markdown(tmp_path, monkeypatch):
    monkeypatch.setattr(leitura.shutil, "which", lambda n: None)
    p = _imagem(tmp_path, "Screenshot_20260923_225121.png")
    assert p.suffix in extractor.SUPPORTED
    md = extractor.extract(p)
    assert md.startswith("# Screenshot_20260923_225121.png") and "2026-09-23T22:51" in md
    assert extractor.format_label(p) == "Imagem"


def test_tessdata_padrao_respeita_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DC_TESSDATA", str(tmp_path))
    assert extractor.tessdata_padrao() == tmp_path


def test_icone_pequeno_nem_roda_ocr(tmp_path, monkeypatch):
    chamou = []
    monkeypatch.setattr(leitura, "ocr", lambda *a, **k: chamou.append(1) or ("texto", ""))
    p = tmp_path / "icone.png"
    Image.new("RGB", (64, 64), "white").save(p)
    i = ler_imagem(p)
    assert chamou == [] and "pequena demais" in i["ocr_motivo"]


def test_fragmento_de_ocr_nao_conta_como_texto(tmp_path, monkeypatch):
    monkeypatch.setattr(leitura, "ocr", lambda *a, **k: ("a b\n c | ok", ""))
    i = ler_imagem(_imagem(tmp_path))
    assert i["texto"] == "" and i["ocr_motivo"] == "OCR achou so fragmentos"
    monkeypatch.setattr(leitura, "ocr", lambda *a, **k: ("ERRO 422", ""))
    assert ler_imagem(_imagem(tmp_path))["texto"] == "ERRO 422"


def test_markdown_tem_texto():
    from delegation_core.imagens import markdown_tem_texto
    assert markdown_tem_texto("# x\n\n## Texto na imagem (OCR)\n\nalgo\n")
    assert not markdown_tem_texto(para_markdown({"nome": "x", "data": "d", "texto": "", "ocr_motivo": "m"}))


def test_cli_ocr_setup_sem_tesseract_nao_quebra(monkeypatch):
    """Portado do fork (test_escopos.py): `console` nao existia no modulo."""
    import shutil
    from types import SimpleNamespace
    from delegation_core import cli
    monkeypatch.setattr(shutil, "which", lambda n: None)
    assert cli.cmd_ocr_setup(SimpleNamespace(langs="por", force=False)) == 1


def test_cli_ocr_setup_baixa_e_termina_sem_quebrar(monkeypatch, tmp_path):
    """Medido em 02/10/2026 ao trazer o OCR do fork para o master: o download
    do primeiro idioma acontecia e o comando morria com NameError ao imprimir o
    tamanho. O teste do fork so cobria o caminho sem tesseract."""
    import shutil
    import urllib.request
    from types import SimpleNamespace
    from delegation_core import cli, config
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/tesseract")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(urllib.request, "urlretrieve",
                        lambda url, alvo: (Path(alvo).write_bytes(b"x" * 2048), None))
    assert cli.cmd_ocr_setup(SimpleNamespace(langs="por,eng", force=False)) == 0
    assert (tmp_path / "tessdata" / "por.traineddata").exists()
    assert (tmp_path / "tessdata" / "eng.traineddata").exists()
