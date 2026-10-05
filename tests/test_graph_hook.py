"""graph_hook.py: git post-commit hook install/uninstall/status, idempotency,
and preserving unrelated hook content — against a real tmp git repo.

_VENV_PYTHON is monkeypatched to sys.executable so this test doesn't depend on
a delegation-core venv actually existing at ~/.delegation_core/venv on whatever
machine runs the suite.
"""

import os
import subprocess
import sys

import pytest

from delegation_core import graph_hook


@pytest.fixture
def repo(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.setattr(graph_hook, "_VENV_PYTHON", __import__("pathlib").Path(sys.executable))
    return tmp_path


def test_status_not_installed_by_default(repo):
    assert graph_hook.status(repo) == {"installed": False}


def test_install_writes_executable_hook(repo):
    result = graph_hook.install(repo, name="my-graph")
    assert result["status"] == "installed"
    hook_path = repo / ".git" / "hooks" / "post-commit"
    assert hook_path.exists()
    if os.name != "nt":  # o Windows nao tem bit de executavel
        assert hook_path.stat().st_mode & 0o111
    content = hook_path.read_text()
    assert "my-graph" in content
    assert str(repo) in content


def test_status_reports_installed_after_install(repo):
    graph_hook.install(repo)
    assert graph_hook.status(repo)["installed"] is True


def test_install_is_idempotent(repo):
    first = graph_hook.install(repo)
    second = graph_hook.install(repo)
    assert first["status"] == "installed"
    assert second["status"] == "already installed"


def test_install_appends_to_existing_hook_without_clobbering_it(repo):
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    existing = hooks_dir / "post-commit"
    existing.write_text("#!/bin/sh\necho 'existing hook content'\n")

    result = graph_hook.install(repo)
    assert result["status"] == "appended to existing post-commit hook"
    content = existing.read_text()
    assert "existing hook content" in content
    assert graph_hook._HOOK_MARKER in content


def test_uninstall_removes_only_our_block_preserves_other_content(repo):
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    existing = hooks_dir / "post-commit"
    existing.write_text("#!/bin/sh\necho 'existing hook content'\n")
    graph_hook.install(repo)

    result = graph_hook.uninstall(repo)
    assert "preserved" in result["status"]
    content = existing.read_text()
    assert "existing hook content" in content
    assert graph_hook._HOOK_MARKER not in content


def test_uninstall_deletes_file_when_nothing_else_remains(repo):
    graph_hook.install(repo)
    result = graph_hook.uninstall(repo)
    assert "deleted" in result["status"]
    assert not (repo / ".git" / "hooks" / "post-commit").exists()


def test_uninstall_when_not_installed_reports_not_installed(repo):
    assert graph_hook.uninstall(repo)["status"] == "not installed"


def test_install_outside_git_repo_returns_error(tmp_path, monkeypatch):
    monkeypatch.setattr(graph_hook, "_VENV_PYTHON", __import__("pathlib").Path(sys.executable))
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    result = graph_hook.install(not_a_repo)
    assert "error" in result


def test_install_missing_venv_python_returns_error(repo, monkeypatch):
    monkeypatch.setattr(graph_hook, "_VENV_PYTHON", repo / "nonexistent-python")
    result = graph_hook.install(repo)
    assert "error" in result
