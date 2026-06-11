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


# --- Phase-2 ship-review R1 regression tests ---


def _mk(s, **kw):
    base = dict(
        name="a",
        description="d",
        type="gotcha",
        status="confirmed",
        opened="2026-01-01",
        body="b",
    )
    base.update(kw)
    s.write_fact(**base)


def test_name_path_traversal_rejected(tmp_path):
    # CB1: --name must not escape memory/
    s = MemoryStore(_root(tmp_path))
    import pytest

    for bad in ("../../evil", "a/b", "..", "/abs", "", "Cap"):
        with pytest.raises(ValueError):
            _mk(s, name=bad)


def test_promote_count1_preserves_body(tmp_path):
    # MAJOR: promote() must flip only the frontmatter status, not a body line
    gf = _root(tmp_path)
    s = MemoryStore(gf)
    _mk(s, name="a", status="pending", body="line1\nstatus: pending\nline3")
    s.promote("a")
    raw = (gf / "memory" / "a.md").read_text()
    fm, body = raw.split("---", 2)[1], raw.split("---", 2)[2]
    assert "status: confirmed" in fm
    assert "status: pending" in body  # body line untouched


def test_promote_rejects_bad_name(tmp_path):
    import pytest

    s = MemoryStore(_root(tmp_path))
    with pytest.raises(ValueError):
        s.promote("../../evil")


def test_stale_domain_registry_removed_on_regen(tmp_path):
    # CB2: deleting a domain-tagged fact must remove its registry file
    gf = _root(tmp_path)
    s = MemoryStore(gf)
    _mk(s, name="x", domain="infra")
    assert (gf / "memory" / "domains" / "infra.md").exists()
    (gf / "memory" / "x.md").unlink()
    s.regenerate()
    assert not (gf / "memory" / "domains" / "infra.md").exists()


def test_absent_index_with_facts_recovers_not_flat_fallback(tmp_path):
    # CM1: index gone but facts on disk (first-write failure) -> regenerate from facts,
    # NOT silently fall back to knowledge.md (which would hide the written facts).
    gf = _root(tmp_path)
    s = MemoryStore(gf)
    _mk(s, name="y", description="distinct-desc")
    (gf / "MEMORY.md").unlink()
    (gf / "knowledge.md").write_text("## Gotchas\n- flat-only content\n")
    out = s.read_index_recovering_stale()
    assert "distinct-desc" in out and "flat-only content" not in out


def test_frontmatter_newline_rejected(tmp_path):
    # CM-major: a newline (or bare ---) in a single-line frontmatter value would
    # make the fact malformed (silently skipped by regenerate); reject at write.
    import pytest

    s = MemoryStore(_root(tmp_path))
    with pytest.raises(ValueError):
        _mk(s, name="a", description="line1\nline2")
    with pytest.raises(ValueError):
        _mk(s, name="b", description="---")


def test_delete_fact_locked_removes_and_regenerates(tmp_path):
    # CB2: invalidation goes through the locked CLI/method, not a raw rm.
    gf = _root(tmp_path)
    s = MemoryStore(gf)
    _mk(s, name="gone", description="to-remove")
    assert (gf / "memory" / "gone.md").exists()
    s.delete_fact("gone")
    assert not (gf / "memory" / "gone.md").exists()
    assert "to-remove" not in (gf / "MEMORY.md").read_text()


def test_delete_fact_rejects_bad_name(tmp_path):
    import pytest

    s = MemoryStore(_root(tmp_path))
    with pytest.raises(ValueError):
        s.delete_fact("../../evil")


def test_write_fact_never_overwrites_suffixes_instead(tmp_path):
    # CB1 (R3): writing an existing name must NOT overwrite — suffix deterministically.
    gf = _root(tmp_path)
    s = MemoryStore(gf)
    _mk(s, name="dup", description="first")
    _mk(s, name="dup", description="second")
    names = sorted(p.stem for p in (gf / "memory").glob("*.md"))
    assert names == ["dup", "dup-2"]
    idx = (gf / "MEMORY.md").read_text()
    assert "first" in idx and "second" in idx  # neither lost


def test_first_rich_write_does_not_overwrite_migrated_fact(tmp_path):
    # CB1 (R3): auto-migrate then a triggering write with a colliding slug must keep both.
    gf = _root(tmp_path)
    (gf / "knowledge.md").write_text("## Gotchas\n- 2026-06-02: cache invalidation legacy\n")
    s = MemoryStore(gf)
    # triggering write whose name collides with the migrated slug "cache-invalidation"
    _mk(s, name="cache-invalidation", description="new triggering learning")
    idx = (gf / "MEMORY.md").read_text()
    assert "cache invalidation legacy" in idx  # migrated legacy fact survived
    assert "new triggering learning" in idx     # triggering fact also present


def test_delete_fact_missing_raises(tmp_path):
    import pytest

    s = MemoryStore(_root(tmp_path))
    with pytest.raises(FileNotFoundError):
        s.delete_fact("nope")


def test_invalid_warn_kb_aborts_before_writing_fact(tmp_path, monkeypatch):
    # CB R4 (P-019 check-act ordering): an invalid GOODFELLOW_MEMORY_WARN_KB must abort
    # write_fact BEFORE the fact file is created — else a retry would suffix a duplicate.
    import pytest

    monkeypatch.setenv("GOODFELLOW_MEMORY_WARN_KB", "abc")
    gf = _root(tmp_path)
    s = MemoryStore(gf)
    with pytest.raises(Exception):
        _mk(s, name="a", description="should-not-persist")
    # no fact file, no dirty marker left behind
    assert not list((gf / "memory").glob("*.md"))
    assert not (gf / "memory" / ".dirty").exists()
