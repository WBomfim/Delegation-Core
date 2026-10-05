"""get_health_summary: generated graph artifacts are excluded from orphan counts.

An orphan is meant to flag a hand-written note that fell out of the knowledge
graph — something worth relinking. graph_build's wiki articles are a different
kind of object: they cross-reference each other with relative markdown links
(``[Community 1](Community_1.md)``) rather than ``[[wikilinks]]``, because they
have to stay valid both inside the vault and as a standalone wiki directory. The
orphan pass only resolves wikilinks, so every generated article looked
unreferenced. Filing one mid-sized repo (graphify, 599 articles) took the vault's
reported orphan count from 63 to 662, drowning the real signal.

They are counted separately under `generated_notes` instead of being silently
dropped, so the number is still visible on the dashboard.
"""

import json

import pytest

from delegation_core.config import Config, vault_health_cache
from delegation_core.vault import VaultManager


def _note(body: str, **fm) -> str:
    lines = "\n".join(f"{k}: {v}" for k, v in fm.items())
    return f"---\n{lines}\n---\n\n{body}"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A vault with one linked note, one true orphan, and two generated articles."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    (tmp_path / "home" / ".delegation_core").mkdir(parents=True)

    cfg = Config(vault_path=str(tmp_path / "vault"), vault_folders=["Reference", "Sessions"])
    ref = cfg.vault / "Reference"
    ref.mkdir(parents=True)

    (ref / "hub.md").write_text(_note("see [[target]]", title="Hub"), encoding="utf-8")
    (ref / "target.md").write_text(_note("linked from hub", title="Target"), encoding="utf-8")
    (ref / "lonely.md").write_text(_note("nobody links here", title="Lonely"), encoding="utf-8")

    gen = ref / "graphs" / "demo"
    gen.mkdir(parents=True)
    for n in (0, 1):
        (gen / f"Community_{n}.md").write_text(
            _note(f"see [Community {1 - n}](Community_{1 - n}.md)",
                  title=f"demo: Community_{n}", source="graph_build"),
            encoding="utf-8")

    manager = VaultManager(cfg)
    manager._ensure_ready = lambda: None
    manager.collection = None
    return manager


def test_generated_articles_are_not_counted_as_orphans(vault):
    health = vault.get_health_summary()

    # hub is linked by nothing, lonely is linked by nothing -> 2 real orphans.
    # target is linked from hub. The two generated articles are excluded.
    assert health["orphans"] == 2
    assert health["generated_notes"] == 2
    assert health["total_notes"] == 5


def test_generated_articles_relative_links_are_not_counted_as_broken(vault):
    """Only [[wikilinks]] are resolved; the articles' markdown links must not
    inflate broken_links either."""
    assert vault.get_health_summary()["broken_links"] == 0


def test_wikilinks_embedded_in_generated_content_are_not_counted_as_broken(vault, tmp_path):
    """A generated report quotes source verbatim, so a `[[...]]` inside one is a
    code sample rather than authored link intent — graphify's report contributed
    a phantom `[[Foo alloc]]` this way."""
    (vault.cfg.vault / "Reference" / "graphs" / "demo" / "report-sample.md").write_text(
        _note("excerpt: `arr[[Foo alloc]]`", title="demo: sample", source="graph_build"),
        encoding="utf-8")
    vault_health_cache().unlink(missing_ok=True)

    health = vault.get_health_summary()

    assert health["broken_links"] == 0
    assert health["generated_notes"] == 3


def test_hand_written_notes_still_count_as_orphans_without_the_marker(vault, tmp_path):
    """Guard against the exclusion widening: only `source: graph_build` opts out."""
    (vault.cfg.vault / "Reference" / "manual.md").write_text(
        _note("no marker here", title="Manual"), encoding="utf-8")
    vault_health_cache().unlink(missing_ok=True)

    health = vault.get_health_summary()

    assert health["orphans"] == 3
    assert health["generated_notes"] == 2


def test_raw_session_transcripts_are_treated_as_generated(vault, tmp_path):
    """The SessionEnd hook dumps a conversation verbatim, so a `[[...]]` inside is
    quoted text. A real transcript of a conversation about the linker contributed
    four phantom targets: [[stem]], [[target]], [[source_stem]], [[new-note-stem]]."""
    sessions = vault.cfg.vault / "Sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "2026-06-29-transcript-52a001bb.md").write_text(
        _note("we rename [[source_stem]] to [[new-note-stem]]",
              title="Raw transcript", **{"type": "session-transcript"}),
        encoding="utf-8")
    vault_health_cache().unlink(missing_ok=True)

    health = vault.get_health_summary()

    assert health["broken_links"] == 0
    assert health["generated_notes"] == 3


def test_links_resolve_against_notes_outside_the_configured_folders(vault, tmp_path):
    """Obsidian resolves a wikilink against every note in the vault. MEMORY.md and
    Vault_Master_Index.md sit at this vault's root, outside vault_folders, and
    links to them were reported broken while opening fine in Obsidian."""
    (vault.cfg.vault / "MEMORY.md").write_text("# root note", encoding="utf-8")
    (vault.cfg.vault / "Reference" / "cites-root.md").write_text(
        _note("see [[MEMORY]]", title="Cites Root"), encoding="utf-8")
    vault_health_cache().unlink(missing_ok=True)

    assert vault.get_health_summary()["broken_links"] == 0


def test_dot_directories_are_excluded_from_link_resolution(vault, tmp_path):
    """.obsidian/ and .chroma_bge/ are machinery, not notes — a link must not
    silently resolve against something Obsidian would never surface."""
    hidden = vault.cfg.vault / ".obsidian" / "plugins"
    hidden.mkdir(parents=True)
    (hidden / "ghost.md").write_text("# not a note", encoding="utf-8")
    (vault.cfg.vault / "Reference" / "cites-hidden.md").write_text(
        _note("see [[ghost]]", title="Cites Hidden"), encoding="utf-8")
    vault_health_cache().unlink(missing_ok=True)

    assert vault.get_health_summary()["broken_links"] == 1


def test_health_summary_is_cached_between_calls(vault, tmp_path):
    """A intencao e o CACHE, e nao onde ele mora.

    Este arquivo se isolava falsificando `Path.home()`, e o codigo de producao
    montava o caminho a partir de `Path.home()` dentro da funcao. As duas coisas
    casavam, e por isso o defeito sobreviveu: um teste que finge ser HOME nao
    percebe que o caminho ignora o CONFIG_DIR do projeto -- e um teste que NAO
    finge (test_frontmatter_valido.py, escrito em 04/09) escreveu no
    vault_health.json real do usuario.
    O caminho agora vem de `config.vault_health_cache()`, e a asercao segue a
    fonte em vez de repetir o caminho a mao.
    """
    first = vault.get_health_summary()
    cache = vault_health_cache()

    assert cache.exists()
    assert json.loads(cache.read_text(encoding="utf-8"))["orphans"] == first["orphans"]


def test_folder_name_markers_are_not_counted_as_broken_links(tmp_path):
    """This vault ends notes with `[[reference]] #digest #pdf` — a categorisation
    marker naming a folder, not a link to a note. 12 of 26 reported broken links
    were these, and no note will ever exist to satisfy them."""
    from delegation_core.config import Config
    from delegation_core.vault import VaultManager

    cfg = Config(vault_path=str(tmp_path), vault_folders=["Reference", "Tools"])
    ref = tmp_path / "Reference"
    ref.mkdir(parents=True)
    (tmp_path / "Tools").mkdir()
    (ref / "marked.md").write_text(
        "---\ntitle: marked\n---\n\nbody\n\n[[reference]] #digest\n", encoding="utf-8")
    (ref / "genuinely-broken.md").write_text(
        "---\ntitle: gb\n---\n\nSee [[note-that-never-existed]].\n", encoding="utf-8")

    health = VaultManager(cfg).get_health_summary()
    assert health["broken_links"] == 1     # the real one only


def test_the_vault_directory_name_is_also_a_marker(tmp_path):
    from delegation_core.config import Config
    from delegation_core.vault import VaultManager

    vault = tmp_path / "Claude Vault"
    (vault / "Reference").mkdir(parents=True)
    cfg = Config(vault_path=str(vault), vault_folders=["Reference"])
    (vault / "Reference" / "n.md").write_text(
        "---\ntitle: n\n---\n\nx\n\n[[Claude Vault]] #decision\n", encoding="utf-8")

    assert VaultManager(cfg).get_health_summary()["broken_links"] == 0


def test_health_cache_is_keyed_by_vault(tmp_path):
    """The cache path is fixed at ~/.delegation_core/vault_health.json, so
    without a vault key a second vault — another profile, or a dashboard sidecar
    pointed elsewhere — is served the first one's numbers for five minutes.
    Found when two tests over different temp vaults returned identical health:
    the second never ran."""
    from delegation_core.config import Config
    from delegation_core.vault import VaultManager

    def build(name, broken_links):
        vault = tmp_path / name
        (vault / "Reference").mkdir(parents=True)
        body = "".join(f"See [[missing-{i}]].\n" for i in range(broken_links))
        (vault / "Reference" / "n.md").write_text(
            f"---\ntitle: n\n---\n\n{body}", encoding="utf-8")
        return Config(vault_path=str(vault), vault_folders=["Reference"])

    first = VaultManager(build("vault-a", 1)).get_health_summary()
    second = VaultManager(build("vault-b", 3)).get_health_summary()

    assert first["broken_links"] == 1
    assert second["broken_links"] == 3


def _vault_with(tmp_path, name="v"):
    from delegation_core.config import Config
    vault = tmp_path / name
    (vault / "Reference").mkdir(parents=True)
    return Config(vault_path=str(vault), vault_folders=["Reference"])


def test_health_detail_itemises_exactly_what_the_summary_counts(tmp_path):
    """The invariant that makes this tool worth having: len(items) == count.
    Three throwaway scripts written to enumerate these reported 248, 63 and 5
    against true values of 31, 31 and 0, each because it re-implemented a
    definition the health pass already owns."""
    from delegation_core.vault import VaultManager

    cfg = _vault_with(tmp_path)
    ref = cfg.vault / "Reference"
    (ref / "a.md").write_text(
        "---\ntitle: a\n---\n\nSee [[ghost-one]] and [[ghost-two]].\n", encoding="utf-8")
    (ref / "b.md").write_text(
        "---\ntitle: b\n---\n\nSee [[a]] and [[ghost-three]].\n", encoding="utf-8")

    vm = VaultManager(cfg)
    detail = vm.health_detail()

    assert detail["broken_links"] == 3
    assert len(detail["broken_link_items"]) == 3
    assert {i["target"] for i in detail["broken_link_items"]} == {
        "ghost-one", "ghost-two", "ghost-three"}
    assert all(i["source"] in {"a", "b"} for i in detail["broken_link_items"])


def test_health_detail_lists_folder_markers_separately_from_broken(tmp_path):
    """Markers are deliberately uncounted; listing them is what stops the next
    reader from trying to 'fix' a link that names a folder."""
    from delegation_core.vault import VaultManager

    cfg = _vault_with(tmp_path, "v2")
    (cfg.vault / "Reference" / "n.md").write_text(
        "---\ntitle: n\n---\n\n[[reference]] #tag\n\nSee [[real-ghost]].\n", encoding="utf-8")

    detail = VaultManager(cfg).health_detail()
    assert detail["broken_links"] == 1
    assert [i["target"] for i in detail["folder_marker_items"]] == ["reference"]


def test_health_detail_caps_lists_but_reports_the_true_total(tmp_path):
    from delegation_core.vault import VaultManager

    cfg = _vault_with(tmp_path, "v3")
    body = "".join(f"[[ghost-{i}]]\n" for i in range(10))
    (cfg.vault / "Reference" / "n.md").write_text(
        f"---\ntitle: n\n---\n\n{body}", encoding="utf-8")

    detail = VaultManager(cfg).health_detail(limit=4)
    assert len(detail["broken_link_items"]) == 4
    assert detail["broken_link_items_total"] == 10
    assert detail["broken_links"] == 10


def test_health_detail_is_not_served_from_another_vaults_pass(tmp_path):
    """The summary cache was unkeyed; detail must not inherit that."""
    from delegation_core.vault import VaultManager

    a = _vault_with(tmp_path, "va")
    (a.vault / "Reference" / "n.md").write_text(
        "---\ntitle: n\n---\n\n[[ghost-a]]\n", encoding="utf-8")
    b = _vault_with(tmp_path, "vb")
    (b.vault / "Reference" / "n.md").write_text(
        "---\ntitle: n\n---\n\n[[ghost-b1]]\n[[ghost-b2]]\n", encoding="utf-8")

    assert VaultManager(a).health_detail()["broken_links"] == 1
    assert VaultManager(b).health_detail()["broken_links"] == 2


def test_link_para_fonte_ingerida_nao_conta_como_quebrado(tmp_path, monkeypatch):
    """Nota que cita arquivo ingerido de fora do vault aponta para algo que o
    servidor acha, entao chamar de quebrado manda alguem consertar o que ja
    funciona.

    Medido neste vault em 24/09/2026: os 9 links quebrados restantes eram todos
    notas citando arquivos de memoria do operador, e todos resolviam.
    """
    from delegation_core import vault as vault_mod
    from delegation_core.vault import VaultManager

    monkeypatch.setattr(
        vault_mod, "_load_registry_for_links",
        lambda: {"/fora/do/vault": {"files": {"/fora/do/vault/feedback_algo.md": [0, 1]}}})

    cfg = _vault_with(tmp_path, "vi1")
    (cfg.vault / "Reference" / "n.md").write_text(
        "---\ntitle: n\n---\n\nVer [[feedback_algo]] e [[fantasma-real]].\n",
        encoding="utf-8")

    detail = VaultManager(cfg).health_detail()
    assert detail["broken_links"] == 1, "so o fantasma conta"
    assert [i["target"] for i in detail["broken_link_items"]] == ["fantasma-real"]
    assert [i["target"] for i in detail["ingested_link_items"]] == ["feedback_algo"]


def test_registro_de_ingestao_ilegivel_nao_derruba_a_checagem(tmp_path, monkeypatch):
    """A checagem de saude nao pode estourar porque o registro de OUTRO
    subsistema corrompeu: um numero levemente pessimista vale mais que nenhum.
    """
    from delegation_core import vault as vault_mod
    from delegation_core.vault import VaultManager

    def explode():
        raise ValueError("registro corrompido")
    monkeypatch.setattr(vault_mod, "_load_registry_for_links", explode)

    cfg = _vault_with(tmp_path, "vi2")
    (cfg.vault / "Reference" / "n.md").write_text(
        "---\ntitle: n\n---\n\nVer [[feedback_algo]].\n", encoding="utf-8")

    detail = VaultManager(cfg).health_detail()
    assert detail["broken_links"] == 1, "sem registro, volta ao comportamento antigo"
    assert detail["ingested_link_items"] == []
