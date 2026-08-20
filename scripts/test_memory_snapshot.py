"""Tests for the memory rollback journal + evidence provenance (MemoryStore).

Every public mutation appends a reversible pre-image entry to
`gf_root/memory/.journal.jsonl` under the single held lock; `rollback()` restores
the affected fact to its pre-image (deleting it if it was newly created) and
regenerates. Facts may carry an optional single-line `evidence:` provenance field.
"""

import errno
import json
import multiprocessing
import os
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


# ---- two-phase WAL: rollback is itself crash-safe (review F2) --------------


def test_rollback_crash_before_fact_op_recovery_reapplies(tmp_path, monkeypatch):
    """Review F2: rollback is TWO-PHASE. A crash BETWEEN the rollback intent journal and
    its fact op leaves a dangling rollback intent; recovery re-applies the fact op
    (restores the deleted fact, re-derived from the forward entry) and commits — never a
    wedged half-rollback surfacing as a CAS conflict on retry."""
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "d", status="confirmed")
    original = (gf / "memory" / "d.md").read_text()
    store.delete_fact("d")
    assert not (gf / "memory" / "d.md").exists()

    fact_path = gf / "memory" / "d.md"
    real_atomic = memory_index._atomic_write

    def crashing(path, text):
        if str(path) == str(fact_path):
            raise RuntimeError("crash before the restore lands")
        return real_atomic(path, text)

    monkeypatch.setattr(memory_index, "_atomic_write", crashing)
    with pytest.raises(RuntimeError):
        store.rollback()  # roll back the delete -> restore d; crashes on the restore write
    monkeypatch.undo()

    assert not fact_path.exists()  # restore never landed
    assert any(
        e["op"] == "rollback" and e.get("committed") is False
        for e in store.read_journal()
    )

    unresolved = MemoryStore(gf).recover()
    assert unresolved == []
    assert fact_path.exists()  # rollback re-applied -> d restored
    assert fact_path.read_text() == original
    rb = [e for e in store.read_journal() if e["op"] == "rollback"]
    assert rb and rb[-1].get("committed") is True  # committed after recovery


# ---- fail-visible recovery of an unresolvable dangler (review F3) -----------


def test_recovery_reports_unresolved_third_state_and_blocks_mutations(tmp_path):
    """Review F3: a dangling intent whose fact was changed OUTSIDE the journal during the
    crash window (third state) cannot be auto-resolved. Recovery must NOT claim success —
    it reports the seq unresolved, never clobbers the fact, the CLI exits non-zero, and
    later mutations FAIL CLOSED until reconciled (R700 / P3 fail-visible)."""
    import subprocess

    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "t", status="confirmed")
    ondisk = (gf / "memory" / "t.md").read_text()
    # Inject a dangling promote intent whose recorded post_hash AND pre_image both differ
    # from the on-disk fact (a genuine third-state edit during the crash window).
    with open(store.journal_path, "a") as f:
        f.write(
            json.dumps(
                {
                    "seq": 500,
                    "op": "promote",
                    "name": "t",
                    "pre_image": "totally-different-preimage",
                    "post_hash": "0" * 64,
                    "committed": False,
                }
            )
            + "\n"
        )

    unresolved = store.recover()
    assert unresolved == [500]  # reported, not silently 'complete'
    assert (gf / "memory" / "t.md").read_text() == ondisk  # never clobbered

    with pytest.raises(MemoryStore.UnresolvedRecovery):
        _fact(store, "another")  # mutations fail closed until reconciled

    r = subprocess.run(
        [
            sys.executable,
            str(pathlib.Path(_HERE) / "memory_index.py"),
            "--root",
            str(gf),
            "recover",
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 1  # CLI fails visibly
    assert "500" in r.stderr


# ---- review round 2: malformed evidence / retention / regen ordering -------


def test_malformed_dangling_write_intent_stays_unresolved(tmp_path):
    """Review-2 F1: a dangling write intent with NO post_hash must not be mistaken for a
    completed mutation just because the fact is absent (None == None). Op-schema
    validation keeps it UNRESOLVED (fail-visible) — never falsely committed."""
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "seed")  # real memory dir + journal
    with open(store.journal_path, "a") as f:
        f.write(
            json.dumps({"seq": 700, "op": "write", "name": "lost", "committed": False})
            + "\n"
        )
    assert store.recover() == [700]
    assert not (gf / "memory" / "lost.md").exists()  # no phantom fact
    lost = [e for e in store.read_journal() if e.get("seq") == 700][0]
    assert lost.get("committed") is False  # NOT flipped committed


def test_rollback_recovery_self_contained_under_retention_1(tmp_path, monkeypatch):
    """Review-2 F2: with GOODFELLOW_JOURNAL_RETENTION=1 the forward entry is evicted the
    moment the rollback intent is appended. Recovery must still complete from the restore
    value EMBEDDED in the rollback intent — never wedge on the missing forward entry."""
    monkeypatch.setenv("GOODFELLOW_JOURNAL_RETENTION", "1")
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "d", status="confirmed")
    original = (gf / "memory" / "d.md").read_text()
    store.delete_fact("d")
    fact_path = gf / "memory" / "d.md"

    real_atomic = memory_index._atomic_write

    def crashing(path, text):
        if str(path) == str(fact_path):
            raise RuntimeError("crash before restore lands")
        return real_atomic(path, text)

    monkeypatch.setattr(memory_index, "_atomic_write", crashing)
    with pytest.raises(RuntimeError):
        store.rollback()  # restore d; crashes on the restore write
    monkeypatch.undo()

    j = store.read_journal()
    assert (
        len(j) == 1 and j[0]["op"] == "rollback"
    )  # forward entry evicted by retention
    assert "restore" in j[0]  # ...but the restore value travels with the intent
    assert MemoryStore(gf).recover() == []
    assert fact_path.read_text() == original  # self-healed despite the eviction


def test_recovery_regen_failure_not_falsely_reported_complete(tmp_path, monkeypatch):
    """Review-2 F3: if recovery commits an intent but then fails to regenerate the index,
    a retry must NOT claim success while .dirty persists — it regenerates whenever dirty
    is set and only clears it once the index is durable."""
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "d", status="confirmed")
    original = (gf / "memory" / "d.md").read_text()
    store.delete_fact("d")
    fact_path = gf / "memory" / "d.md"

    real_atomic = memory_index._atomic_write

    def crash_restore(path, text):
        if str(path) == str(fact_path):
            raise RuntimeError("crash before restore lands")
        return real_atomic(path, text)

    monkeypatch.setattr(memory_index, "_atomic_write", crash_restore)
    with pytest.raises(RuntimeError):
        store.rollback()  # -> dangling rollback intent, fact absent
    monkeypatch.undo()

    def boom_regen(*a, **k):
        raise RuntimeError("regen storage full")

    monkeypatch.setattr(memory_index, "regenerate", boom_regen)
    with pytest.raises(RuntimeError):
        MemoryStore(gf).recover()  # re-applies + commits, then regen fails -> raises
    monkeypatch.undo()
    assert fact_path.read_text() == original  # fact restored + committed
    assert store.dirty_path.exists()  # but the index is NOT durable yet

    # Retry: intent already committed (changed would be False), but the dirty marker
    # forces a regenerate; success is only reported once .dirty clears.
    assert MemoryStore(gf).recover() == []
    assert not store.dirty_path.exists()
    assert "d" in (gf / "MEMORY.md").read_text()


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


# ---- review round 3: F1 legacy-journal upgrade across a crash --------------


def test_legacy_interrupted_forward_is_rolled_back(tmp_path):
    """Review-3 F1: a PRE-two-phase version crashed after journaling a forward write but
    before the fact landed, then the install upgrades. The legacy entry has NO ``committed``
    field; treating it as committed would let recovery report success while journal and
    fact disagree (later CAS conflict + retention evicting the evidence). Recovery must
    infer state on the latest-per-name legacy entry: the fact never landed -> roll back."""
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "seed")  # a real memory dir + a committed journal entry
    j = store.read_journal()
    next_seq = max(e["seq"] for e in j) + 1
    legacy = {  # single-phase forward entry: no ``committed`` key
        "seq": next_seq,
        "op": "write",
        "name": "lost",
        "pre_image": None,
        "post_hash": memory_index._content_hash("a body that never landed\n"),
        "ts": "2026-01-01T00:00:00+00:00",
    }
    with store.journal_path.open("a") as f:
        f.write(json.dumps(legacy) + "\n")
    assert not (gf / "memory" / "lost.md").exists()

    store2 = MemoryStore(gf)
    assert store2.recover() == []  # resolved, NOT reported unresolved
    assert not (gf / "memory" / "lost.md").exists()  # never resurrected
    rb = [
        e
        for e in store2.read_journal()
        if e["op"] == "rollback" and e.get("target_seq") == next_seq
    ]
    assert rb and rb[-1].get("committed") is True  # rolled back via a committed marker
    _fact(store2, "after")  # mutations no longer fail closed


def _legacy_write_entry(seq, name, post_text, ts):
    """A single-phase (no ``committed`` field) forward write journal entry."""
    return {
        "seq": seq,
        "op": "write",
        "name": name,
        "pre_image": None,
        "post_hash": memory_index._content_hash(post_text),
        "ts": ts,
    }


def test_legacy_journal_all_completed_recovers_to_committed_noop(tmp_path):
    """Review-3 F1 (no false-positive): a healthy pre-two-phase journal — every entry
    lacks ``committed`` and every fact matches its entry's post-image — recovers to a
    benign no-op: each latest-per-name entry rolls FORWARD to committed, nothing flagged."""
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "one")
    _fact(store, "two")
    entries = store.read_journal()
    for e in entries:  # simulate a legacy journal: strip every ``committed`` flag
        e.pop("committed", None)
    store.journal_path.write_text("".join(json.dumps(e) + "\n" for e in entries))

    store2 = MemoryStore(gf)
    assert store2.recover() == []  # all facts present + matching -> clean
    for e in store2.read_journal():
        assert e.get("committed") is True  # each rolled forward
    _fact(store2, "after")  # not failed closed


def test_legacy_older_interrupted_forward_is_resolved_not_globally_newest(tmp_path):
    """Review-4 F2-r4: an interrupted legacy mutation is NOT necessarily the globally
    newest entry — a crashed pre-two-phase process releases its flock and a LATER process
    can journal a successful mutation after it. Recovery must inspect the latest-per-name
    legacy entry (not just the global newest): here the OLDER write 'x' never landed and
    is rolled back, the NEWER write 'y' landed and commits — recover() must NOT falsely
    report 'complete' while leaving x's phantom unreconciled (R700)."""
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "y")  # produces a real y.md whose content we can hash
    y_hash = memory_index._content_hash((gf / "memory" / "y.md").read_text())
    x = _legacy_write_entry(
        1, "x", "x that never landed\n", "2026-01-01T00:00:00+00:00"
    )
    y = {  # newer legacy write, fact present + matching
        "seq": 2,
        "op": "write",
        "name": "y",
        "pre_image": None,
        "post_hash": y_hash,
        "ts": "2026-01-02T00:00:00+00:00",
    }
    store.journal_path.write_text(json.dumps(x) + "\n" + json.dumps(y) + "\n")
    assert not (gf / "memory" / "x.md").exists()  # x's mutation never landed

    store2 = MemoryStore(gf)
    assert store2.recover() == []  # older phantom RESOLVED (rolled back), not ignored
    assert not (gf / "memory" / "x.md").exists()
    rb = [
        e
        for e in store2.read_journal()
        if e["op"] == "rollback" and e.get("target_seq") == 1
    ]
    assert rb and rb[-1].get("committed") is True  # x rolled back via committed marker
    y_entry = [
        e for e in store2.read_journal() if e["op"] == "write" and e["name"] == "y"
    ]
    assert y_entry and y_entry[0].get("committed") is True  # y rolled forward
    _fact(store2, "after")  # not failed closed


def test_legacy_third_state_non_journaled_edit_is_unresolved(tmp_path):
    """Review-4 F2-r4 (fail-visible): a latest-per-name legacy entry whose fact is a
    genuine THIRD state (neither post- nor pre-image — a non-journaled edit during the
    crash window) must stay UNRESOLVED and block mutations, never a bogus 'complete'."""
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "z")
    entries = store.read_journal()
    for e in entries:
        e.pop("committed", None)
    store.journal_path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    (gf / "memory" / "z.md").write_text("# non-journaled corruption\n")  # third state

    store2 = MemoryStore(gf)
    assert store2.recover() == [1]  # reported unresolved, not silently 'complete'
    with pytest.raises(MemoryStore.UnresolvedRecovery):
        _fact(store2, "blocked")  # mutations fail closed until reconciled


# ---- review round 3: F2 directory-fsync failure propagation ----------------


def test_fsync_dir_propagates_real_io_error_but_skips_unsupported(
    tmp_path, monkeypatch
):
    """Review-3 F2: a real directory-fsync failure (EIO/ENOSPC) is a durability failure
    the WAL depends on -> PROPAGATE (fail loud). A filesystem that cannot fsync a dir fd
    (EINVAL/ENOTSUP) is genuinely unsupported -> skip best-effort (never claim durability
    it cannot provide, never crash a mutation on an unsupported platform)."""

    def fsync_eio(fd):
        raise OSError(errno.EIO, "simulated device IO error")

    monkeypatch.setattr(memory_index.os, "fsync", fsync_eio)
    with pytest.raises(OSError) as ei:
        memory_index._fsync_dir(str(tmp_path))
    assert ei.value.errno == errno.EIO
    monkeypatch.undo()

    def fsync_einval(fd):
        raise OSError(errno.EINVAL, "directory fsync not supported here")

    monkeypatch.setattr(memory_index.os, "fsync", fsync_einval)
    memory_index._fsync_dir(str(tmp_path))  # unsupported -> best-effort skip, no raise


def test_mutation_fails_loud_when_dir_fsync_hits_real_io_error(tmp_path, monkeypatch):
    """Review-3 F2 end-to-end: a real dir-fsync failure during a mutation surfaces as a
    raised error rather than a silent success that falsely claims host-crash durability."""
    gf = _root(tmp_path)
    store = MemoryStore(gf)

    def fsync_eio(fd):
        raise OSError(errno.EIO, "simulated device IO error")

    monkeypatch.setattr(memory_index.os, "fsync", fsync_eio)
    with pytest.raises(OSError):
        _fact(store, "boom")


# ---- review round 3: F3 durable domain-registry removal --------------------


def test_last_domain_delete_fsyncs_the_registry_dir(tmp_path, monkeypatch):
    """Review-3 F3: deleting the last domain-tagged fact purges its registry with a raw
    unlink; without a directory fsync a power loss can resurrect the stale registry and
    re-surface an invalidated learning via the rich domain-recall path. The purge must
    fsync the domains/ directory (parity with the durable fact unlink)."""
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "d1", domain="process")
    reg = gf / "memory" / "domains" / "process.md"
    assert reg.exists()

    domains_dir = str(gf / "memory" / "domains")
    fsynced = []
    real = memory_index._fsync_dir

    def spy(path):
        fsynced.append(str(path))
        return real(path)

    monkeypatch.setattr(memory_index, "_fsync_dir", spy)
    store.delete_fact("d1")  # last domain fact -> registry purged
    assert not reg.exists()
    assert domains_dir in fsynced  # the removal was made durable (F3)


# ---- review round 4: F1-r4 retried rollback must not roll back a 2nd fact ---


def test_retried_rollback_does_not_roll_back_a_second_fact(tmp_path, monkeypatch):
    """Review-4 F1-r4 / P32: rollback() is two-phase; if it applies its fact op and the
    process crashes before the commit flip, RETRYING rollback() must complete the SAME
    rollback and stop — never recover the in-flight rollback and then continue into
    default-target selection to roll back the next unreverted fact. With two facts a
    retried default rollback would otherwise remove both."""
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "a")  # seq 1
    _fact(store, "b")  # seq 2 (the default rollback target)

    # Crash the FIRST rollback() after its fact op but before the commit marker.
    def crash_commit(self, seq):
        raise RuntimeError("simulated crash before the rollback commit marker")

    monkeypatch.setattr(MemoryStore, "_mark_committed_locked", crash_commit)
    with pytest.raises(RuntimeError):
        store.rollback()  # targets b; applies the delete, then crashes on commit
    monkeypatch.undo()

    assert not (gf / "memory" / "b.md").exists()  # b's fact op landed
    dangling = [
        e
        for e in store.read_journal()
        if e["op"] == "rollback" and e.get("committed") is False
    ]
    assert dangling and dangling[-1]["target_seq"] == 2

    # RETRY: a fresh process re-invokes the same default rollback.
    store2 = MemoryStore(gf)
    result = store2.rollback()
    assert result == 2  # returns the seq it (re)completed — b
    assert not (gf / "memory" / "b.md").exists()  # b stays rolled back
    assert (gf / "memory" / "a.md").exists()  # a is NOT collaterally rolled back
    rb = [e for e in store2.read_journal() if e["op"] == "rollback"]
    assert (
        rb[-1].get("committed") is True
    )  # in-flight rollback committed, no new target


# ---- review round 4: F3-r4 directory-OPEN failure propagation --------------


def test_fsync_dir_propagates_open_io_error_on_posix(tmp_path, monkeypatch):
    """Review-4 F3-r4: a real os.open() failure on a directory this code just wrote to
    (EIO, EMFILE/ENFILE fd exhaustion, ...) is a genuine error the WAL depends on — on
    POSIX it must PROPAGATE, not be swallowed as an 'unsupported platform' skip."""
    if os.name != "posix":  # pragma: no cover - suite runs on the POSIX VPS
        pytest.skip("POSIX-only propagation semantics")
    real_open = memory_index.os.open

    def open_eio(path, *a, **k):
        if str(path) == str(tmp_path):
            raise OSError(errno.EIO, "simulated directory open IO error")
        return real_open(path, *a, **k)

    monkeypatch.setattr(memory_index.os, "open", open_eio)
    with pytest.raises(OSError) as ei:
        memory_index._fsync_dir(str(tmp_path))
    assert ei.value.errno == errno.EIO


def test_legacy_same_name_write_then_promote_survives_repeated_recover(tmp_path):
    """Review-5 F2-r5 / P17: a HEALTHY legacy history with multiple forward mutations of
    ONE name (write pending -> promote confirmed) must recover to a clean no-op and stay
    clean across REPEATED recover() calls. The candidate is the latest FORWARD entry per
    name across all entries (not the latest still-fieldless one), so once the promote
    commits, the older superseded write must NOT resurface as a false 'latest legacy'
    candidate and wedge future mutations."""
    gf = _root(tmp_path)
    store = MemoryStore(gf)
    _fact(store, "f", status="pending")  # seq 1: write
    store.promote("f")  # seq 2: promote -> confirmed on disk
    entries = store.read_journal()
    for e in entries:  # simulate a pre-two-phase journal
        e.pop("committed", None)
    store.journal_path.write_text("".join(json.dumps(e) + "\n" for e in entries))

    store2 = MemoryStore(gf)
    assert store2.recover() == []  # latest (promote) rolls forward; write untouched
    assert store2.recover() == []  # SECOND pass must NOT wedge on the superseded write
    assert store2.recover() == []  # ...and stays clean
    assert "status: confirmed" in (gf / "memory" / "f.md").read_text()
    _fact(store2, "after")  # mutations are NOT failed closed
