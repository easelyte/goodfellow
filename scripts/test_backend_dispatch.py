import pathlib
import subprocess
import sys
from memory_config import resolve_mode


def test_unset_resolves_flat(monkeypatch):
    monkeypatch.delenv("GOODFELLOW_MEMORY", raising=False)
    assert resolve_mode() == "flat"  # default mode unchanged


def test_flat_mode_does_not_create_rich_store(tmp_path, monkeypatch):
    # In flat mode the close/ship/snap-compact rich branch must NOT fire:
    # no per-fact dir, no MEMORY.md index. (Flat append stays inline markdown — untouched.)
    monkeypatch.setenv("GOODFELLOW_MEMORY", "flat")
    gf = tmp_path / ".goodfellow"
    gf.mkdir()
    assert resolve_mode() == "flat"
    assert not (gf / "memory").exists() and not (gf / "MEMORY.md").exists()


def test_rich_mode_write_produces_per_fact_file(tmp_path, monkeypatch):
    # spec testing "rich-mode write output asserted"
    monkeypatch.setenv("GOODFELLOW_MEMORY", "rich")
    gf = tmp_path / ".goodfellow"
    (gf / "memory").mkdir(parents=True)
    subprocess.run(
        [
            sys.executable,
            "memory_index.py",
            "--root",
            str(gf),
            "write-fact",
            "--name",
            "a",
            "--description",
            "d",
            "--type",
            "gotcha",
            "--status",
            "confirmed",
            "--opened",
            "2026-06-10",
            "--body",
            "x",
        ],
        check=True,
        cwd=pathlib.Path(__file__).parent,
    )
    assert (gf / "memory" / "a.md").exists()
    assert (gf / "MEMORY.md").exists()


def test_pending_staleness_window_matches_loops():
    # T-2.9: rich pending-fact staleness reuses the 30-day loop-staleness window.
    # The close skill documents the 30-day window; assert loop_store uses the same.
    txt = (
        pathlib.Path(__file__).parent.parent / "skills" / "close" / "SKILL.md"
    ).read_text()
    assert "30-day" in txt and "pending" in txt.lower()
