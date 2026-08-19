"""Tests for the memory rollback journal + evidence provenance (MemoryStore).

Every public mutation appends a reversible pre-image entry to
`gf_root/memory/.journal.jsonl` under the single held lock; `rollback()` restores
the affected fact to its pre-image (deleting it if it was newly created) and
regenerates. Facts may carry an optional single-line `evidence:` provenance field.
"""

import json
import multiprocessing
import pathlib
import sys

_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import pytest  # noqa: E402

import memory_index  # noqa: E402
from memory_index import MemoryStore, migrate, validate_fact  # noqa: E402


def _root(tmp):
    gf = pathlib.Path(tmp) / ".goodfellow"
    (gf / "memory").mkdir(parents=True)
    return gf


def _fact(store, name, status="pending", **kw):
    store.write_fact(
        name=name,
        description=f"desc {name}",
        type="principle",
        status=status,
        opened="2026-08-16",
        **kw,
    )


# ---- evidence provenance --------------------------------------------------


def test_write_fact_records_evidence_frontmatter(tmp_path):
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "cite-boundary", evidence="commit-abc123 (design-doc)")
    fm_text = (gf / "memory" / "cite-boundary.md").read_text()
    assert "evidence: commit-abc123 (design-doc)" in fm_text
    assert validate_fact(gf / "memory" / "cite-boundary.md")


def test_evidence_is_optional(tmp_path):
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "no-cite")
    fm_text = (gf / "memory" / "no-cite.md").read_text()
    assert "evidence:" not in fm_text
    assert validate_fact(gf / "memory" / "no-cite.md")


def test_evidence_must_be_single_line(tmp_path):
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    with pytest.raises(ValueError):
        _fact(store, "bad-cite", evidence="line1\nline2")


# ---- journal records each mutation ----------------------------------------


def test_write_appends_journal_entry_with_null_preimage(tmp_path):
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "alpha")
    journal = store.read_journal()
    assert len(journal) == 1
    e = journal[0]
    assert e["op"] == "write"
    assert e["name"] == "alpha"
    assert e["pre_image"] is None
    assert e["seq"] == 1


def test_promote_and_delete_capture_preimage(tmp_path):
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "beta", status="pending")
    store.promote("beta")
    store.delete_fact("beta")
    ops = [(e["op"], e["name"]) for e in store.read_journal()]
    assert ops == [("write", "beta"), ("promote", "beta"), ("delete", "beta")]
    promote_entry = store.read_journal()[1]
    assert "status: pending" in promote_entry["pre_image"]
    delete_entry = store.read_journal()[2]
    assert "status: confirmed" in delete_entry["pre_image"]


def test_seqs_are_monotonic(tmp_path):
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "a")
    _fact(store, "b")
    _fact(store, "c")
    seqs = [e["seq"] for e in store.read_journal()]
    assert seqs == [1, 2, 3]


# ---- rollback -------------------------------------------------------------


def test_rollback_write_deletes_created_fact(tmp_path):
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "gamma")
    assert (gf / "memory" / "gamma.md").exists()
    store.rollback()
    assert not (gf / "memory" / "gamma.md").exists()
    # index regenerated without it
    assert "gamma" not in (gf / "MEMORY.md").read_text()
    # rollback is itself journaled
    assert store.read_journal()[-1]["op"] == "rollback"
    assert store.read_journal()[-1]["target_seq"] == 1


def test_rollback_delete_restores_fact(tmp_path):
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "delta", status="confirmed")
    original = (gf / "memory" / "delta.md").read_text()
    store.delete_fact("delta")
    assert not (gf / "memory" / "delta.md").exists()
    store.rollback()
    assert (gf / "memory" / "delta.md").read_text() == original


def test_rollback_promote_restores_pending(tmp_path):
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "epsilon", status="pending")
    store.promote("epsilon")
    assert "status: confirmed" in (gf / "memory" / "epsilon.md").read_text()
    store.rollback()
    assert "status: pending" in (gf / "memory" / "epsilon.md").read_text()


def test_rollback_targets_specific_seq(tmp_path):
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "one")  # seq 1
    _fact(store, "two")  # seq 2
    store.rollback(seq=1)  # undo the FIRST write, not the last
    assert not (gf / "memory" / "one.md").exists()
    assert (gf / "memory" / "two.md").exists()


def test_rollback_same_seq_twice_raises(tmp_path):
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "solo")
    store.rollback(seq=1)
    with pytest.raises(ValueError):
        store.rollback(seq=1)


def test_rollback_with_empty_journal_raises(tmp_path):
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    with pytest.raises(ValueError):
        store.rollback()


def test_rollback_skips_already_reverted_for_default_target(tmp_path):
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "p")  # seq 1
    _fact(store, "q")  # seq 2
    store.rollback()  # undoes seq 2 (q)
    store.rollback()  # should undo seq 1 (p), not re-target seq 2
    assert not (gf / "memory" / "p.md").exists()
    assert not (gf / "memory" / "q.md").exists()


# ---- migration must not spam the journal ----------------------------------


def test_migration_does_not_journal_each_fact(tmp_path):
    """Bulk flat->rich migration writes via _write_fact_file directly, so it must
    not append a per-fact journal entry — journaling lives in the public methods."""
    gf = _root(tmp_path)
    (gf / "knowledge.md").write_text(
        "## Principles\n- 2026-08-16: legacy learning one\n- 2026-08-16: legacy learning two\n"
    )
    migrate(gf)
    migrated = [p for p in (gf / "memory").glob("*.md") if not p.name.startswith(".")]
    assert len(migrated) == 2  # migration produced facts
    assert MemoryStore(gf).read_journal() == []  # but journaled nothing


# ---- concurrency: journal stays consistent under parallel writers ---------


def _worker(gf, name):
    sys.path.insert(0, _HERE)
    from memory_index import MemoryStore as MS

    MS(gf).write_fact(
        name=name,
        description=f"desc {name}",
        type="principle",
        status="pending",
        opened="2026-08-16",
    )


def test_concurrent_writers_produce_unique_seqs(tmp_path):
    gf = _root(tmp_path)
    procs = [
        multiprocessing.Process(target=_worker, args=(str(gf), f"w{i}"))
        for i in range(4)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    store = MemoryStore(gf)
    journal = store.read_journal()
    assert len(journal) == 4
    seqs = sorted(e["seq"] for e in journal)
    assert seqs == [1, 2, 3, 4]  # lock serialized them; no collision/gap


# ---- CLI round-trip -------------------------------------------------------


def test_rollback_old_write_blocked_by_later_same_name(tmp_path):
    """F1/P19: rolling back an old write of `x` after `x` was deleted+recreated must
    NOT clobber the newer file — the later same-name mutation must be undone first."""
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "x")  # seq 1 (write)
    store.delete_fact("x")  # seq 2
    _fact(store, "x")  # seq 3 (write again — newer state)
    with pytest.raises(ValueError, match="later mutation"):
        store.rollback(seq=1)
    assert (gf / "memory" / "x.md").exists()  # newer file untouched


def test_delete_journal_failure_does_not_lose_the_fact(tmp_path, monkeypatch):
    """F1/F2/P19: the journal (pre-image) is written BEFORE the destructive unlink, so
    if journaling fails the fact is NOT deleted — the learning is never lost. .dirty is
    set (write-ahead), so a fresh read recovers a consistent index that still lists it."""
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "doomed", status="confirmed")
    assert "doomed" in (gf / "MEMORY.md").read_text()

    def boom(*a, **k):
        raise RuntimeError("journal storage full")

    monkeypatch.setattr(store, "_journal_locked", boom)
    with pytest.raises(RuntimeError):
        store.delete_fact("doomed")
    assert (gf / "memory" / "doomed.md").exists()  # NOT lost — journal-before-unlink
    assert store.dirty_path.exists()  # write-ahead marker survived
    # A fresh reader recovers to a consistent index that still lists the surviving fact.
    recovered = MemoryStore(gf).read_index_recovering_stale()
    assert "doomed" in recovered
    assert not store.dirty_path.exists()  # cleared after successful regen


def test_rollback_rejects_path_traversal_name(tmp_path):
    """F3/P33: a crafted journal entry with a traversing name must not unlink/write
    outside memory/. NAME_RE + containment reject it before any filesystem op."""
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "seed")  # create memory dir + a real journal
    outside = gf / "evil.md"
    outside.write_text("do not touch")
    # Inject a malicious entry (valid JSON, traversing name) into the journal.
    with open(store.journal_path, "a") as f:
        f.write(
            json.dumps({"seq": 99, "op": "write", "name": "../evil", "pre_image": None})
            + "\n"
        )
    with pytest.raises(ValueError):
        store.rollback(seq=99)
    assert outside.read_text() == "do not touch"  # untouched


def test_journal_retention_caps_entries(tmp_path, monkeypatch):
    """F4/P53: the journal is bounded to the retention window; seqs stay monotonic
    because the newest entries are kept."""
    monkeypatch.setenv("GOODFELLOW_JOURNAL_RETENTION", "3")
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    for n in ("a", "b", "c", "d", "e"):
        _fact(store, n)
    journal = store.read_journal()
    assert len(journal) == 3
    assert [e["seq"] for e in journal] == [3, 4, 5]


def test_promote_already_confirmed_raises_without_journaling(tmp_path):
    """F5/P54: promoting an already-confirmed fact is not a transition — it must raise
    and NOT append a no-op journal entry a later rollback could 'succeed' on."""
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "conf", status="confirmed")
    with pytest.raises(ValueError, match="not pending"):
        store.promote("conf")
    ops = [e["op"] for e in store.read_journal()]
    assert ops == ["write"]  # no phantom promote entry


def test_carriage_return_in_evidence_rejected(tmp_path):
    """F2/P8: a CR (or any splitlines separator) in a frontmatter value would inject a
    second frontmatter line — must be rejected, not just LF."""
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    for bad in ("a\rstatus: confirmed", "a\x0bb", "a\x85b"):
        with pytest.raises(ValueError):
            _fact(store, "cr-fact", evidence=bad)
    assert not list((gf / "memory").glob("cr-fact*.md"))  # nothing published


def test_rollback_conflict_on_nonjournaled_edit(tmp_path):
    """F3/P19: if a fact is edited outside the journal after its mutation, rollback
    compare-and-swap must refuse rather than clobber the newer content."""
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "zeta", status="confirmed")
    # Simulate a non-journaled external edit of the fact file.
    (gf / "memory" / "zeta.md").write_text(
        "---\nname: zeta\ndescription: hand edited\ntype: principle\n"
        "status: confirmed\nopened: 2026-08-16\n---\nnew body\n"
    )
    with pytest.raises(ValueError, match="conflict"):
        store.rollback()
    assert "hand edited" in (gf / "memory" / "zeta.md").read_text()  # not clobbered


def test_interior_journal_corruption_fails_loud(tmp_path):
    """F4/P3: a torn FINAL line is tolerated, but a corrupt INTERIOR line means lost
    history and must raise rather than silently drop an entry."""
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "j1")
    _fact(store, "j2")
    lines = store.journal_path.read_text().splitlines()
    # Corrupt the FIRST (interior) line, keep a valid last line.
    store.journal_path.write_text("{bogus not json\n" + lines[-1] + "\n")
    with pytest.raises(MemoryStore.JournalCorruption):
        store.read_journal()
    # A torn FINAL line alone is tolerated.
    store.journal_path.write_text(lines[0] + "\n{truncated")
    ok = store.read_journal()
    assert len(ok) == 1


def test_valid_json_wrong_shape_is_rejected(tmp_path):
    """F3/P33: a syntactically valid but wrong-shaped interior line (e.g. `[]`, or a
    dict missing seq/op) must raise, not crash downstream with AttributeError."""
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "k1")
    good_last = store.journal_path.read_text().splitlines()[-1]
    # Interior wrong-shape (a JSON array) + a valid final line.
    store.journal_path.write_text("[]\n" + good_last + "\n")
    with pytest.raises(MemoryStore.JournalCorruption):
        store.read_journal()
    # Interior dict missing required fields.
    store.journal_path.write_text('{"foo": 1}\n' + good_last + "\n")
    with pytest.raises(MemoryStore.JournalCorruption):
        store.read_journal()
    # A wrong-shape FINAL line alone is tolerated (torn write that happened to parse).
    store.journal_path.write_text(good_last + "\n[]")
    assert len(store.read_journal()) == 1


def test_journal_byte_budget_drops_oldest(tmp_path, monkeypatch):
    """F5/P53: retention also bounds total bytes — a large pre-image can't grow the log
    without limit; oldest entries drop until under budget (never below one)."""
    monkeypatch.setenv("GOODFELLOW_JOURNAL_MAX_BYTES", "400")
    monkeypatch.setenv("GOODFELLOW_JOURNAL_RETENTION", "100")
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    for n in ("a", "b", "c", "d", "e"):
        _fact(store, n, body="x" * 100)  # each entry carries a chunky post-mutation
    journal = store.read_journal()
    assert 1 <= len(journal) < 5  # bounded well below the count cap by the byte budget
    assert len(store.journal_path.read_text().encode()) <= 400 or len(journal) == 1


# ---- two-phase WAL: crash recovery (PART A) ------------------------------


def test_crash_between_intent_and_fact_write_recovers_cleanly(tmp_path, monkeypatch):
    """PART A: a crash BETWEEN the intent journal append and the fact write leaves a
    dangling intent (committed:false) whose recorded post_hash does NOT match on-disk
    state (the fact never landed). On single-phase code rollback() surfaces this as an
    unrecoverable 'reconcile manually' CAS conflict. Two-phase recovery must resolve it
    cleanly: the write never landed, so it is rolled back (fact stays absent)."""
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    fact_path = gf / "memory" / "wal.md"

    real_atomic = memory_index._atomic_write

    def crashing_atomic(path, text):
        if str(path) == str(fact_path):
            raise RuntimeError("simulated crash before the fact lands")
        return real_atomic(path, text)

    monkeypatch.setattr(memory_index, "_atomic_write", crashing_atomic)
    with pytest.raises(RuntimeError):
        _fact(store, "wal")
    monkeypatch.undo()  # crash is over; a fresh process restarts

    # Dangling intent recorded, but the fact never landed.
    j = store.read_journal()
    assert any(e["op"] == "write" and e.get("committed") is False for e in j)
    assert not fact_path.exists()

    # Recovery on read must NOT raise and must NOT resurrect a half-written fact.
    store2 = MemoryStore(gf)
    idx = store2.read_index_recovering_stale()
    assert "wal" not in idx

    # An explicit rollback must resolve to the benign 'nothing to roll back' — NOT the
    # unrecoverable CAS 'reconcile manually' conflict single-phase code raises here.
    with pytest.raises(ValueError) as ei:
        store2.rollback()
    assert "reconcile manually" not in str(ei.value)
    assert "no journaled mutation" in str(ei.value)


def test_crash_after_fact_before_commit_rolls_forward(tmp_path, monkeypatch):
    """PART A: a crash AFTER the fact + index land but BEFORE the commit marker leaves a
    dangling intent whose post_hash MATCHES on-disk state. Recovery must roll it FORWARD
    (mark committed, keep the fact), never delete a fact that actually landed."""
    gf = _root(tmp_path)
    store = MemoryStore(gf)

    def crash_commit(self, seq):
        raise RuntimeError("simulated crash before the commit marker")

    monkeypatch.setattr(MemoryStore, "_mark_committed_locked", crash_commit)
    with pytest.raises(RuntimeError):
        _fact(store, "landed", status="confirmed")
    monkeypatch.undo()

    fact_path = gf / "memory" / "landed.md"
    assert fact_path.exists()  # the fact DID land + index regenerated
    j = store.read_journal()
    assert any(
        e["op"] == "write" and e["name"] == "landed" and e.get("committed") is False
        for e in j
    )

    store2 = MemoryStore(gf)
    store2.recover()
    w = [
        e for e in store2.read_journal() if e["op"] == "write" and e["name"] == "landed"
    ]
    assert w and w[0].get("committed") is True  # rolled forward -> committed
    assert fact_path.exists()  # fact preserved, never clobbered


# ---- two-phase WAL: byte budget (PART B) --------------------------------


def test_byte_budget_two_phase_keeps_in_flight_committed(tmp_path, monkeypatch):
    """PART B: under a tiny byte budget, truncation drops the OLDEST entries but always
    retains the newest in-flight record — and a completed mutation's record carries its
    commit marker (committed:true). Seqs stay monotonic across truncation."""
    monkeypatch.setenv("GOODFELLOW_JOURNAL_MAX_BYTES", "400")
    monkeypatch.setenv("GOODFELLOW_JOURNAL_RETENTION", "100")
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    prev = 0
    for n in ("a", "b", "c", "d", "e"):
        _fact(store, n)
        j = store.read_journal()
        assert j, "journal never dropped below one entry"
        newest = j[-1]
        # the just-written mutation's record survives truncation AND is committed
        assert newest["op"] == "write"
        assert newest.get("committed") is True
        # seqs strictly increase across truncation (max(seq)+1 preserved)
        assert newest["seq"] > prev
        prev = newest["seq"]
    # budget meaningfully enforced (well under the count cap of 100)
    assert len(store.read_journal()) < 5


def test_byte_budget_single_huge_preimage_is_kept(tmp_path, monkeypatch):
    """PART B documented exception: if a SINGLE entry's pre_image alone exceeds the byte
    budget we still KEEP it — losing it would forfeit the ability to recover the mutation
    in flight. Facts are soft-capped far below the default budget, so this is the
    pathological corner, deliberately resolved in favour of durability over the bound."""
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    huge = "y" * 5000
    _fact(store, "big", status="confirmed", body=huge)
    monkeypatch.setenv(
        "GOODFELLOW_JOURNAL_MAX_BYTES", "500"
    )  # smaller than the pre_image
    store.delete_fact("big")  # pre_image (the whole fact incl. huge body) > 500 bytes
    j = store.read_journal()
    assert j, "the in-flight delete record must not be evicted below one entry"
    assert j[-1]["op"] == "delete"
    assert huge in (j[-1]["pre_image"] or "")  # huge pre_image retained intact


def test_cli_write_evidence_and_rollback(tmp_path):
    import subprocess

    gf = _root(tmp_path)
    base = [
        sys.executable,
        str(pathlib.Path(_HERE) / "memory_index.py"),
        "--root",
        str(gf),
    ]
    r = subprocess.run(
        base
        + [
            "write-fact",
            "--name",
            "cli-fact",
            "--description",
            "via cli",
            "--type",
            "gotcha",
            "--status",
            "pending",
            "--opened",
            "2026-08-16",
            "--evidence",
            "commit-abc123",
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "evidence: commit-abc123" in (gf / "memory" / "cli-fact.md").read_text()

    j = subprocess.run(base + ["journal"], capture_output=True, text=True)
    assert j.returncode == 0
    assert json.loads(j.stdout.splitlines()[0])["op"] == "write"

    rb = subprocess.run(base + ["rollback"], capture_output=True, text=True)
    assert rb.returncode == 0, rb.stderr
    assert not (gf / "memory" / "cli-fact.md").exists()
