"""Tests for the principle-file resolver + CLI (T-1.4)."""

import subprocess
import sys
import pathlib

import pytest

from principles_context import resolve_principle_files, ConfigError

SCRIPTS = pathlib.Path(__file__).resolve().parent


def _seed(tmp_path):
    """Write empty principles.md / principles-web.md under a knowledge/ dir;
    return the plugin_root that contains it."""
    kn = tmp_path / "knowledge"
    kn.mkdir(parents=True, exist_ok=True)
    (kn / "principles.md").write_text("")
    (kn / "principles-web.md").write_text("")
    return tmp_path


def test_core_only_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("GOODFELLOW_PRINCIPLES_WEB", raising=False)
    files = resolve_principle_files(plugin_root=_seed(tmp_path), project_root=tmp_path)
    assert files == ["principles.md"]


def test_web_enabled_by_env_1(tmp_path, monkeypatch):
    monkeypatch.setenv("GOODFELLOW_PRINCIPLES_WEB", "1")
    files = resolve_principle_files(plugin_root=_seed(tmp_path), project_root=tmp_path)
    assert files == ["principles.md", "principles-web.md"]


def test_web_autodetect_package_json(tmp_path, monkeypatch):
    monkeypatch.delenv("GOODFELLOW_PRINCIPLES_WEB", raising=False)
    (tmp_path / "package.json").write_text("{}")
    files = resolve_principle_files(plugin_root=_seed(tmp_path), project_root=tmp_path)
    assert "principles-web.md" in files


@pytest.mark.parametrize("bad", ["true", "yes", "0", "1 "])
def test_invalid_web_value_hard_errors(tmp_path, monkeypatch, bad):
    monkeypatch.setenv("GOODFELLOW_PRINCIPLES_WEB", bad)
    with pytest.raises(ConfigError):
        resolve_principle_files(plugin_root=_seed(tmp_path), project_root=tmp_path)


def test_empty_env_behaves_as_unset_autodetect(tmp_path, monkeypatch):
    # CB-R5-1 contract: empty string == unset == autodetect (no package.json -> core only).
    monkeypatch.setenv("GOODFELLOW_PRINCIPLES_WEB", "")
    files = resolve_principle_files(plugin_root=_seed(tmp_path), project_root=tmp_path)
    assert files == ["principles.md"]


def test_missing_core_seed_hard_errors(tmp_path, monkeypatch):
    # R6: core principles.md is mandatory; a missing core must fail loud at resolution,
    # not rely solely on the skill cat-loop (which previously could read only web).
    monkeypatch.delenv("GOODFELLOW_PRINCIPLES_WEB", raising=False)
    (tmp_path / "knowledge").mkdir()  # knowledge/ exists but principles.md absent
    with pytest.raises(ConfigError):
        resolve_principle_files(plugin_root=tmp_path, project_root=tmp_path)


def test_forced_web_with_file_absent_hard_errors(tmp_path, monkeypatch):
    # CM-R5-1: explicit GOODFELLOW_PRINCIPLES_WEB=1 but the supplement isn't shipped
    # -> packaging drift, must fail loud (NOT silently fall back to core-only).
    monkeypatch.setenv("GOODFELLOW_PRINCIPLES_WEB", "1")
    kn = tmp_path / "knowledge"
    kn.mkdir()
    (kn / "principles.md").write_text("")
    with pytest.raises(ConfigError):
        resolve_principle_files(plugin_root=tmp_path, project_root=tmp_path)


def test_autodetect_with_web_file_absent_not_appended(tmp_path, monkeypatch):
    # M1: package.json autodetect turns web ON, but the web file isn't present ->
    # the exists() gate still wins, only core is returned (no crash).
    monkeypatch.delenv("GOODFELLOW_PRINCIPLES_WEB", raising=False)
    (tmp_path / "package.json").write_text("{}")
    kn = tmp_path / "knowledge"
    kn.mkdir()
    (kn / "principles.md").write_text("")
    files = resolve_principle_files(plugin_root=tmp_path, project_root=tmp_path)
    assert files == ["principles.md"]


# --- CLI (skills invoke via CLI, not import) ---


def test_cli_prints_core_only(tmp_path, monkeypatch):
    _seed(tmp_path)
    monkeypatch.delenv("GOODFELLOW_PRINCIPLES_WEB", raising=False)
    env = {**_clean_env(monkeypatch), "CLAUDE_PLUGIN_ROOT": str(tmp_path)}
    r = subprocess.run(
        [sys.executable, "principles_context.py", "--project-root", str(tmp_path)],
        cwd=SCRIPTS,
        capture_output=True,
        text=True,
        env=env,
    )
    assert r.returncode == 0
    assert r.stdout.split() == ["principles.md"]


def test_cli_prints_web_when_enabled(tmp_path, monkeypatch):
    _seed(tmp_path)
    env = {
        **_clean_env(monkeypatch),
        "CLAUDE_PLUGIN_ROOT": str(tmp_path),
        "GOODFELLOW_PRINCIPLES_WEB": "1",
    }
    r = subprocess.run(
        [sys.executable, "principles_context.py", "--project-root", str(tmp_path)],
        cwd=SCRIPTS,
        capture_output=True,
        text=True,
        env=env,
    )
    assert r.returncode == 0
    assert r.stdout.split() == ["principles.md", "principles-web.md"]


def test_cli_hard_errors_on_invalid_env(tmp_path, monkeypatch):
    _seed(tmp_path)
    env = {
        **_clean_env(monkeypatch),
        "CLAUDE_PLUGIN_ROOT": str(tmp_path),
        "GOODFELLOW_PRINCIPLES_WEB": "true",
    }
    r = subprocess.run(
        [sys.executable, "principles_context.py", "--project-root", str(tmp_path)],
        cwd=SCRIPTS,
        capture_output=True,
        text=True,
        env=env,
    )
    assert r.returncode != 0
    assert "GOODFELLOW_PRINCIPLES_WEB" in r.stderr


def _clean_env(monkeypatch):
    import os

    e = dict(os.environ)
    e.pop("GOODFELLOW_PRINCIPLES_WEB", None)
    return e
