"""extractor: which extensions become text, and which are silently nothing.

An extension outside SUPPORTED never becomes an ingest candidate at all: it is
filtered before extraction and reported only as a count. So a format that reads
as plain text but is missing from the set is content the vault cannot see and
does not say it is missing: openclaw's docs skipped three real deploy guides
because they were written as .mdx.
"""

import json
import pytest

from delegation_core.extractor import SUPPORTED, extract

MDX = """---
summary: "Deploy OpenClaw on Railway with one-click template"
---

# Railway

<Card title="One-click deploy" href="https://railway.app">
  Click deploy, set OPENCLAW_TOKEN, done.
</Card>
"""


def test_mdx_is_extractable_because_it_is_markdown(tmp_path):
    """MDX is markdown with JSX interleaved; the prose is the point and the
    components degrade to inert tags."""
    f = tmp_path / "railway.mdx"
    f.write_text(MDX, encoding="utf-8")

    text = extract(f)

    assert text is not None
    assert "Deploy OpenClaw on Railway" in text
    assert "set OPENCLAW_TOKEN" in text


def test_mdx_is_in_supported_or_ingest_never_offers_it(tmp_path):
    """extract() alone is not enough: ingest globs on SUPPORTED before calling it."""
    assert ".mdx" in SUPPORTED


def test_json_is_in_supported_and_extracts(tmp_path):
    """JSON files (exports, schemas, design systems) are supported and formatted."""
    assert ".json" in SUPPORTED
    f = tmp_path / "design_system.json"
    f.write_text('{"name": "Soteria", "version": 1, "tokens": ["color", "spacing"]}', encoding="utf-8")

    text = extract(f)
    assert text is not None
    assert '"name": "Soteria"' in text
    assert '"tokens": [\n    "color",\n    "spacing"\n  ]' in text


@pytest.mark.parametrize("suffix", [".svg", ".js", ".css"])
def test_formats_that_are_deliberately_not_documents(tmp_path, suffix):
    """Script and style files carry no document prose: they would add rows without content.

    Raster images left this list on 27/09/2026: with OCR, 75 of 78 real
    screenshots carried text. The concern this test encoded still holds for
    images WITHOUT text, and ingest.py now skips those (test_ingest_reporting)."""
    f = tmp_path / f"asset{suffix}"
    f.write_text("{}", encoding="utf-8")

    assert suffix not in SUPPORTED
    assert extract(f) is None
