"""Config dataclass: property derivation, engine-mode routing, load/save roundtrip.

load()/save() read module-level CONFIG_DIR/CONFIG_FILE globals (not per-instance
state), so any test touching them monkeypatches those globals to a tmp_path first
— otherwise a test run would read/overwrite the real ~/.delegation_core/config.json.
"""

from pathlib import Path

from delegation_core.config import Config


def test_derived_paths_follow_vault_path():
    cfg = Config(vault_path="/tmp/some-vault")
    assert cfg.vault == Path("/tmp/some-vault")
    assert cfg.chroma_path == Path("/tmp/some-vault/.chroma_bge")


def test_graphs_dir_and_registry_are_under_config_dir():
    cfg = Config()
    assert cfg.graphs_dir.name == "graphs"
    assert cfg.graphs_registry_path.name == "graphs_registry.json"
    assert cfg.graphs_dir.parent == cfg.graphs_registry_path.parent


def test_is_configured_requires_vault_binary_and_model():
    assert not Config().is_configured()
    assert not Config(vault_path="/x").is_configured()
    assert Config(vault_path="/x", llama_binary="/y", llama_model="/z").is_configured()
    assert Config(vault_path="/x", engine_mode="agent").is_configured()


def test_is_cpu_budget_and_mode_flags():
    assert Config(budget_mode="cpu").is_cpu_budget
    assert not Config(budget_mode="normal").is_cpu_budget
    assert Config(engine_mode="agent").is_agent_mode
    assert Config(engine_mode="hybrid").is_hybrid_mode
    assert Config(engine_mode="local").uses_local_model
    assert Config(engine_mode="hybrid").uses_local_model
    assert not Config(engine_mode="agent").uses_local_model


def test_route_local_mode_always_local():
    cfg = Config(engine_mode="local")
    assert cfg.route(task="anything") == "local"


def test_route_agent_mode_always_agent():
    cfg = Config(engine_mode="agent")
    assert cfg.route(task="anything") == "agent"


def test_route_hybrid_heavy_tasks_always_local():
    cfg = Config(engine_mode="hybrid")
    assert cfg.route(task="synthesize", input_chars=10) == "local"
    assert cfg.route(task="heal", input_chars=10) == "local"


def test_route_hybrid_big_interactive_input_offers_local():
    cfg = Config(engine_mode="hybrid", hybrid_local_min_chars=100)
    assert cfg.route(task="search_summary", input_chars=500) == "offer"
    assert cfg.route(task="search_summary", input_chars=10) == "agent"


def test_route_hybrid_explicit_use_local_wins():
    cfg = Config(engine_mode="hybrid")
    assert cfg.route(task="search_summary", input_chars=10, use_local=True) == "local"


def test_load_returns_defaults_when_no_file(monkeypatch, tmp_path):
    import delegation_core.config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")

    cfg = config_mod.Config.load()
    assert cfg.vault_path == ""
    assert cfg.server_url == "http://127.0.0.1:8797/mcp"


def test_load_preserves_existing_mcp_port(monkeypatch, tmp_path):
    import json
    import delegation_core.config as config_mod

    config_file = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", config_file)

    for port in (8787, 9999):
        config_file.write_text(json.dumps({"server_port": port}), encoding="utf-8")
        cfg = config_mod.Config.load()
        assert cfg.server_url == f"http://127.0.0.1:{port}/mcp"

    config_file.write_text("{}", encoding="utf-8")
    assert config_mod.Config.load().server_url == "http://127.0.0.1:8797/mcp"


def test_save_then_load_roundtrips(monkeypatch, tmp_path):
    import delegation_core.config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")

    original = config_mod.Config(vault_path="/my/vault", budget_mode="cpu", synthesis_lang="pt")
    original.save()

    reloaded = config_mod.Config.load()
    assert reloaded.vault_path == "/my/vault"
    assert reloaded.budget_mode == "cpu"
    assert reloaded.synthesis_lang == "pt"


def test_ingest_configuration_roundtrips(monkeypatch, tmp_path):
    import delegation_core.config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    original = config_mod.Config(
        ingest_sources=[{"name": "docs", "path": "/tmp/docs", "enabled": True}],
        ingest_exclude_patterns=[".git", "*.log"],
    )

    original.save()

    loaded = config_mod.Config.load()
    assert loaded.ingest_sources == original.ingest_sources
    assert loaded.ingest_exclude_patterns == [".git", "*.log"]


def test_load_reads_ingest_configuration_from_json(monkeypatch, tmp_path):
    import json
    import delegation_core.config as config_mod

    config_file = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", config_file)
    config_file.write_text(json.dumps({
        "ingest_sources": [{"name": "docs", "path": "C:/docs", "enabled": True}],
        "ingest_exclude_patterns": [".git", "*.log"],
    }), encoding="utf-8")

    loaded = config_mod.Config.load()

    assert loaded.ingest_sources[0]["name"] == "docs"
    assert loaded.ingest_exclude_patterns == [".git", "*.log"]


def test_load_corrupt_file_falls_back_to_defaults_not_raises(monkeypatch, tmp_path):
    """A truncated/corrupt config.json (crash mid-write, manual edit, disk full)
    must not take down every tool that calls Config.load() at startup — it
    should log and fall back to defaults, same resilience convention as
    graphbridge._load_registry and tracker.ProcessTracker._read."""
    import delegation_core.config as config_mod

    config_file = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", config_file)
    config_file.write_text("{not valid json", encoding="utf-8")

    cfg = config_mod.Config.load()
    assert cfg.vault_path == ""


def test_load_ignores_unknown_keys(monkeypatch, tmp_path):
    import json
    import delegation_core.config as config_mod

    config_file = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", config_file)

    config_file.write_text(json.dumps({"vault_path": "/x", "some_removed_field": "junk"}))
    cfg = config_mod.Config.load()
    assert cfg.vault_path == "/x"
    assert not hasattr(cfg, "some_removed_field")


def test_load_calibrates_threshold_to_model_profile_when_omitted(monkeypatch, tmp_path):
    import json
    import delegation_core.config as config_mod

    config_file = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", config_file)

    config_file.write_text(json.dumps({"vault_path": "/x", "bge_model": "BAAI/bge-m3"}))
    cfg = config_mod.Config.load()
    assert cfg.bge_model == "BAAI/bge-m3"
    assert cfg.search_threshold == 0.45
