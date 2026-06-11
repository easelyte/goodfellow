import pathlib
import multiprocessing
import subprocess
import sys
from memory_index import MemoryStore


def _root(tmp):
    r = tmp / ".goodfellow" / "memory"
    r.mkdir(parents=True)
    return tmp / ".goodfellow"


def test_atomic_write_same_dir_no_partial(tmp_path):
    gf = _root(tmp_path)
    s = MemoryStore(gf)
    s.write_fact(
        name="a",
        description="d",
        type="gotcha",
        status="confirmed",
        opened="2026-06-02",
        body="x",
    )
    assert (gf / "memory" / "a.md").exists()
    assert not list((gf / "memory").glob("*.tmp"))  # no temp left behind
    assert (gf / "MEMORY.md").exists()  # index at root, not under memory/


def _w(gf, n):
    MemoryStore(gf).write_fact(
        name=n,
        description="d",
        type="gotcha",
        status="confirmed",
        opened="2026-06-02",
        body="x",
    )


def test_concurrent_writers_both_present(tmp_path):
    gf = _root(tmp_path)
    ps = [multiprocessing.Process(target=_w, args=(gf, n)) for n in ("a", "b")]
    [p.start() for p in ps]
    [p.join() for p in ps]
    idx = (gf / "MEMORY.md").read_text()
    assert "a" in idx and "b" in idx  # no stale-index omission


def test_dirty_marker_and_read_recovers(tmp_path):
    gf = _root(tmp_path)
    s = MemoryStore(gf)
    s.write_fact(
        name="a",
        description="d",
        type="gotcha",
        status="confirmed",
        opened="2026-06-02",
        body="x",
    )
    (gf / "memory" / ".dirty").write_text("")  # simulate failed regen
    assert s.read_index_recovering_stale()
    assert not (gf / "memory" / ".dirty").exists()


def test_stale_by_mtime_regenerates(tmp_path):
    import os
    import time

    gf = _root(tmp_path)
    s = MemoryStore(gf)
    s.write_fact(
        name="a",
        description="d",
        type="gotcha",
        status="confirmed",
        opened="2026-06-02",
        body="x",
    )
    # write a new fact file directly (no regen) and bump its mtime past MEMORY.md
    (gf / "memory" / "b.md").write_text(
        "---\nname: b\ndescription: dd\ntype: gotcha\nstatus: confirmed\nopened: 2026-06-02\n---\nbody\n"
    )
    future = time.time() + 100
    os.utime(gf / "memory" / "b.md", (future, future))
    idx = s.read_index_recovering_stale()
    assert "b" in idx  # stale index regenerated to include b


def test_absent_index_falls_back_to_knowledge(tmp_path):
    gf = _root(tmp_path)
    (gf / "knowledge.md").write_text("## Gotchas\n- 2026-06-02: legacy\n")
    s = MemoryStore(gf)
    # no MEMORY.md, no facts -> fallback reads knowledge.md
    out = s.read_index_recovering_stale()
    assert "legacy" in out


def test_migrating_sentinel_reads_knowledge_no_regen(tmp_path):
    gf = _root(tmp_path)
    (gf / "knowledge.md").write_text("## Gotchas\n- 2026-06-02: legacy\n")
    (gf / "memory" / ".migrating").write_text("")
    s = MemoryStore(gf)
    out = s.read_index_recovering_stale()
    assert "legacy" in out
    assert not (gf / "MEMORY.md").exists()  # did NOT regenerate a partial index


def test_promote_pending_to_confirmed(tmp_path):
    gf = _root(tmp_path)
    s = MemoryStore(gf)
    s.write_fact(
        name="a",
        description="d",
        type="gotcha",
        status="pending",
        opened="2026-06-02",
        body="x",
    )
    s.promote("a")
    from memory_index import _parse_frontmatter

    assert _parse_frontmatter(gf / "memory" / "a.md")["status"] == "confirmed"


def test_domain_rejected_at_write(tmp_path):
    gf = _root(tmp_path)
    s = MemoryStore(gf)
    import pytest

    with pytest.raises(ValueError):
        s.write_fact(
            name="a",
            description="d",
            type="gotcha",
            status="confirmed",
            opened="2026-06-02",
            domain="../../README",
            body="x",
        )


def test_cli_write_and_read_roundtrip(tmp_path):
    gf = _root(tmp_path)
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
            "2026-06-02",
            "--body",
            "x",
        ],
        check=True,
        cwd=pathlib.Path(__file__).parent,
    )
    out = subprocess.run(
        [sys.executable, "memory_index.py", "--root", str(gf), "read-index"],
        capture_output=True,
        text=True,
        cwd=pathlib.Path(__file__).parent,
    )
    assert "a" in out.stdout
    # read-index stdout is data-only (V7): no WARN lines
    assert "WARN" not in out.stdout
