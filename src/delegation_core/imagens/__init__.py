"""Imagem vira texto buscavel (EXIF + OCR). Pacote independente: ver `leitura.py`."""

from .leitura import (EXTENSOES, data_da_captura, idiomas_disponiveis, ler_imagem, markdown_tem_texto, ocr,
                      para_markdown)

__all__ = ["EXTENSOES", "data_da_captura", "idiomas_disponiveis", "ler_imagem", "markdown_tem_texto", "ocr",
           "para_markdown"]
