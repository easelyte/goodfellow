import pytest
from recall_pointer import pointer_or_none


def test_pointer_only_when_rich_and_index_present(tmp_path, monkeypatch):
    monkeypatch.setenv("GOODFELLOW_MEMORY", "rich")
    gf = tmp_path / ".goodfellow"
    gf.mkdir()
    (gf / "MEMORY.md").write_text("a\nb\n")
    out = pointer_or_none(gf)
    assert out is not None
    assert "Goodfellow memory" in out


def test_no_pointer_in_flat(tmp_path, monkeypatch):
    monkeypatch.setenv("GOODFELLOW_MEMORY", "flat")
    gf = tmp_path / ".goodfellow"
    gf.mkdir()
    (gf / "MEMORY.md").write_text("a\n")
    assert pointer_or_none(gf) is None


def test_no_pointer_when_index_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("GOODFELLOW_MEMORY", "rich")
    gf = tmp_path / ".goodfellow"
    gf.mkdir()
    assert pointer_or_none(gf) is None  # rich but no MEMORY.md


def test_pointer_checks_gf_root_not_memory_subdir(tmp_path, monkeypatch):
    # MA-2: param is gf_root; MEMORY.md is at gf_root/MEMORY.md, NOT gf_root/memory/
    monkeypatch.setenv("GOODFELLOW_MEMORY", "rich")
    gf = tmp_path / ".goodfellow"
    (gf / "memory").mkdir(parents=True)
    (gf / "memory" / "MEMORY.md").write_text("x\n")  # wrong location
    assert pointer_or_none(gf) is None
    (gf / "MEMORY.md").write_text("a\nb\nc\n")  # right location
    assert pointer_or_none(gf) is not None


def test_invalid_mode_no_crash(tmp_path, monkeypatch):
    # bad config -> guard returns None rather than crashing the SessionStart hook
    monkeypatch.setenv("GOODFELLOW_MEMORY", "ritch")
    gf = tmp_path / ".goodfellow"
    gf.mkdir()
    (gf / "MEMORY.md").write_text("a\n")
    assert pointer_or_none(gf) is None
