import pytest
import subprocess
import sys
import pathlib
from memory_config import resolve_mode, warn_kb, ConfigError


@pytest.mark.parametrize(
    "val,exp", [(None, "flat"), ("flat", "flat"), ("rich", "rich")]
)
def test_mode_ok(monkeypatch, val, exp):
    monkeypatch.delenv("GOODFELLOW_MEMORY", raising=False)
    if val is not None:
        monkeypatch.setenv("GOODFELLOW_MEMORY", val)
    assert resolve_mode() == exp


def test_empty_resolves_flat(monkeypatch):
    monkeypatch.setenv("GOODFELLOW_MEMORY", "")
    assert resolve_mode() == "flat"


@pytest.mark.parametrize("bad", ["Rich", "rich ", "ritch", "1", "FLAT"])
def test_mode_hard_error(monkeypatch, bad):
    monkeypatch.setenv("GOODFELLOW_MEMORY", bad)
    with pytest.raises(ConfigError):
        resolve_mode()


@pytest.mark.parametrize("bad", ["abc", "0", "-1", "16.5"])
def test_warn_kb_hard_error(monkeypatch, bad):
    monkeypatch.setenv("GOODFELLOW_MEMORY_WARN_KB", bad)
    with pytest.raises(ConfigError):
        warn_kb()


def test_warn_kb_default(monkeypatch):
    monkeypatch.delenv("GOODFELLOW_MEMORY_WARN_KB", raising=False)
    assert warn_kb() == 16


def test_warn_kb_positive(monkeypatch):
    monkeypatch.setenv("GOODFELLOW_MEMORY_WARN_KB", "32")
    assert warn_kb() == 32


def test_cli_resolve_mode_valid(monkeypatch):
    monkeypatch.setenv("GOODFELLOW_MEMORY", "rich")
    out = subprocess.run(
        [sys.executable, "memory_config.py", "resolve-mode"],
        capture_output=True,
        text=True,
        cwd=pathlib.Path(__file__).parent,
    )
    assert out.returncode == 0
    assert out.stdout.strip() == "rich"


def test_cli_resolve_mode_invalid(monkeypatch):
    import os

    env = dict(os.environ, GOODFELLOW_MEMORY="ritch")
    out = subprocess.run(
        [sys.executable, "memory_config.py", "resolve-mode"],
        capture_output=True,
        text=True,
        cwd=pathlib.Path(__file__).parent,
        env=env,
    )
    assert out.returncode != 0
    assert "GOODFELLOW_MEMORY" in out.stderr


def test_warn_kb_is_reexport_of_memory_index():
    # CM-R4-1: warn_kb lives in memory_index; memory_config re-exports it.
    import memory_index

    assert warn_kb is memory_index.warn_kb
    assert ConfigError is memory_index.ConfigError
