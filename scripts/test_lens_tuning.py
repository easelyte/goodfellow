"""Tests for lens_tuning.py — the read-only reviewer-lens-tuning MVP.

The analyzer joins the two DURABLE outcome stores (loops.json + triage-log.jsonl)
and surfaces review `source`s whose SURVIVING deferred findings were mostly
triaged not-a-defect, or mostly operator-overridden — as a human-attention
pointer, NOT a lens error rate. It only points; a human edits the lens prose.

The metric is deliberately narrow, because the underlying data is a biased,
erodable subsample:
- Denominator = findings DEFERRED to loops only (ship files loops for deferred
  findings; findings fixed inline during review, and polish-tier deferred
  findings filed as gotchas, never enter the stores). So it is a deferred-loop
  rejection rate, not the lens's false-positive rate.
- not-a-defect CLOSES a loop; retention prunes old closed-loop triage entries
  while active (real-defect) loops persist — so rejection counts are a FLOOR.
- operator_override is a direction-less boolean (disagreement, not proven noise).

These tests pin the join, the two signal gates, the sample gate, and that the
report declares every one of those validity limits.
"""

import json
import pathlib
import sys

_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import lens_tuning  # noqa: E402
import loop_store  # noqa: E402
import triage_helper  # noqa: E402


def _loop(id, source, status="closed"):
    return {"id": id, "title": f"loop-{id}", "source": source, "status": status}


def _triage(loop_id, decision, operator_override=False):
    return {
        "loop_id": loop_id,
        "decision": decision,
        "operator_override": operator_override,
    }


# ---- pure aggregation -----------------------------------------------------


def test_attribute_by_source_counts_decisions():
    loops = [
        _loop(1, "ship-review-r1"),
        _loop(2, "ship-review-r1"),
        _loop(3, "ship-review-r2"),
    ]
    triage = [
        _triage(1, "not-a-defect"),
        _triage(2, "real-defect"),
        _triage(3, "not-a-defect", operator_override=True),
    ]
    stats = lens_tuning.attribute_by_source(loops, triage)
    r1 = stats["ship-review-r1"]
    assert r1.total == 2
    assert r1.triaged == 2
    assert r1.not_a_defect == 1
    assert r1.real_defect == 1
    assert r1.rejection_ratio == 0.5
    r2 = stats["ship-review-r2"]
    assert r2.not_a_defect == 1
    assert r2.operator_override == 1
    assert r2.rejection_ratio == 1.0
    assert r2.override_ratio == 1.0


def test_latest_triage_decision_wins_per_loop():
    """A re-triaged loop uses its most recent decision (append-only chronology)."""
    loops = [_loop(1, "ship-review-r1")]
    triage = [_triage(1, "real-defect"), _triage(1, "not-a-defect")]
    stats = lens_tuning.attribute_by_source(loops, triage)
    r1 = stats["ship-review-r1"]
    assert r1.not_a_defect == 1
    assert r1.real_defect == 0
    assert r1.triaged == 1


def test_untriaged_loops_counted_but_not_scored():
    loops = [_loop(1, "ship-review-r1"), _loop(2, "ship-review-r1")]
    triage = [_triage(1, "not-a-defect")]
    stats = lens_tuning.attribute_by_source(loops, triage)
    r1 = stats["ship-review-r1"]
    assert r1.total == 2
    assert r1.triaged == 1
    assert r1.rejection_ratio == 1.0  # 1 of 1 triaged; untriaged excluded from ratio


def test_loops_without_source_are_skipped():
    loops = [_loop(1, None), _loop(2, "ship-review-r1")]
    triage = [_triage(1, "not-a-defect"), _triage(2, "not-a-defect")]
    stats = lens_tuning.attribute_by_source(loops, triage)
    assert None not in stats
    assert "ship-review-r1" in stats


# ---- signal gates ---------------------------------------------------------


def test_suggestion_requires_min_sample():
    """A single triaged finding must not trigger a suggestion."""
    loops = [_loop(1, "ship-review-r1")]
    triage = [_triage(1, "not-a-defect")]
    stats = lens_tuning.attribute_by_source(loops, triage)
    sugg = lens_tuning.suggest_lens_tweaks(stats, min_sample=3)
    assert sugg == []


def test_rejection_signal_fires_above_threshold():
    loops = [_loop(i, "ship-review-r1") for i in range(1, 5)]
    triage = [
        _triage(1, "not-a-defect"),
        _triage(2, "not-a-defect"),
        _triage(3, "not-a-defect"),
        _triage(4, "real-defect"),
    ]
    stats = lens_tuning.attribute_by_source(loops, triage)
    sugg = lens_tuning.suggest_lens_tweaks(stats, min_sample=3, reject_threshold=0.5)
    assert len(sugg) == 1
    assert sugg[0].source == "ship-review-r1"
    assert "deferred-rejection" in sugg[0].signals
    assert sugg[0].lens_location is not None
    assert "codex-bridge.sh" in sugg[0].lens_location


def test_override_signal_fires_independently_of_rejection():
    """F3: operator-override is a real, gating signal — not merely cosmetic.
    A source with 0 not-a-defect but a high override rate must still surface."""
    loops = [_loop(i, "ship-review-r1") for i in range(1, 5)]
    triage = [
        _triage(1, "real-defect", operator_override=True),
        _triage(2, "real-defect", operator_override=True),
        _triage(3, "real-defect", operator_override=True),
        _triage(4, "real-defect"),
    ]
    stats = lens_tuning.attribute_by_source(loops, triage)
    assert stats["ship-review-r1"].rejection_ratio == 0.0  # no not-a-defect
    sugg = lens_tuning.suggest_lens_tweaks(
        stats, min_sample=3, reject_threshold=0.5, override_threshold=0.5
    )
    assert len(sugg) == 1
    assert "operator-disagreement" in sugg[0].signals
    assert "deferred-rejection" not in sugg[0].signals


def test_low_signal_source_yields_no_suggestion():
    loops = [_loop(i, "ship-review-r1") for i in range(1, 5)]
    triage = [_triage(i, "real-defect") for i in range(1, 5)]
    stats = lens_tuning.attribute_by_source(loops, triage)
    sugg = lens_tuning.suggest_lens_tweaks(stats, min_sample=3)
    assert sugg == []


def test_unknown_source_still_suggests_but_flags_no_lens_map():
    loops = [_loop(i, "mystery-source") for i in range(1, 5)]
    triage = [_triage(i, "not-a-defect") for i in range(1, 4)] + [
        _triage(4, "real-defect")
    ]
    stats = lens_tuning.attribute_by_source(loops, triage)
    sugg = lens_tuning.suggest_lens_tweaks(stats, min_sample=3)
    assert len(sugg) == 1
    assert sugg[0].lens_location is None


# ---- honesty of the rendered report --------------------------------------


def test_report_declares_all_validity_limits():
    loops = [_loop(i, "ship-review-r1") for i in range(1, 5)]
    triage = [
        _triage(1, "not-a-defect"),
        _triage(2, "not-a-defect"),
        _triage(3, "not-a-defect"),
        _triage(4, "real-defect"),
    ]
    stats = lens_tuning.attribute_by_source(loops, triage)
    sugg = lens_tuning.suggest_lens_tweaks(stats)
    report = lens_tuning.render_report(sugg, stats)
    flat = " ".join(report.split()).lower()
    # source-level, not per-lens
    assert "not per-lens" in flat or "not lens-granular" in flat
    # F2: deferred-only denominator — not a lens false-positive rate
    assert "deferred" in flat
    assert (
        "not the lens" in flat
        or "not a lens false-positive" in flat
        or "not a lens error" in flat
    )
    # F1: retention floor / under-reporting
    assert "floor" in flat or "under-report" in flat or "pruned" in flat
    # F3: override direction-less
    assert "direction" in flat
    # under-firing out of scope
    assert "under-fir" in flat or "missed" in flat
    # read-only
    assert "suggest" in flat


def test_json_report_is_machine_readable():
    loops = [_loop(i, "ship-review-r1") for i in range(1, 5)]
    triage = [_triage(i, "not-a-defect") for i in range(1, 4)] + [
        _triage(4, "real-defect")
    ]
    stats = lens_tuning.attribute_by_source(loops, triage)
    sugg = lens_tuning.suggest_lens_tweaks(stats)
    out = lens_tuning.render_report(sugg, stats, as_json=True)
    parsed = json.loads(out)
    assert "suggestions" in parsed
    assert "caveats" in parsed
    assert parsed["suggestions"][0]["source"] == "ship-review-r1"
    assert "deferred-rejection" in parsed["suggestions"][0]["signals"]


# ---- end-to-end over the real durable stores ------------------------------


def test_load_outcomes_reads_real_stores(tmp_path):
    root = str(tmp_path)
    for i in range(1, 5):
        loop_store.add_loop(
            title=f"finding-{i}", source="ship-review-r1", project_root=root
        )
    for lid, dec in [
        (1, "not-a-defect"),
        (2, "not-a-defect"),
        (3, "not-a-defect"),
        (4, "real-defect"),
    ]:
        triage_helper.log_decision({"loop_id": lid, "decision": dec}, project_root=root)
    loops, triage = lens_tuning.load_outcomes(project_root=root)
    stats = lens_tuning.attribute_by_source(loops, triage)
    assert stats["ship-review-r1"].total == 4
    assert stats["ship-review-r1"].not_a_defect == 3
    sugg = lens_tuning.suggest_lens_tweaks(stats)
    assert len(sugg) == 1


def test_retention_pruning_biases_rejection_toward_floor(tmp_path):
    """F1: not-a-defect closes loops whose triage entries retention later prunes,
    while real-defect loops persist. Simulate the prune (drop pruned loops + their
    triage entries) and confirm the surviving rejection ratio DROPS — i.e. the
    metric under-reports rejection, which is exactly why the report calls it a floor."""
    root = str(tmp_path)
    for i in range(1, 5):
        loop_store.add_loop(
            title=f"finding-{i}", source="ship-review-r1", project_root=root
        )
    decisions = [
        (1, "not-a-defect"),
        (2, "not-a-defect"),
        (3, "not-a-defect"),
        (4, "real-defect"),
    ]
    for lid, dec in decisions:
        triage_helper.log_decision({"loop_id": lid, "decision": dec}, project_root=root)

    loops, triage = lens_tuning.load_outcomes(project_root=root)
    full = lens_tuning.attribute_by_source(loops, triage)["ship-review-r1"]
    assert full.rejection_ratio == 0.75  # 3 of 4 before pruning

    # Retention removes the two OLDEST closed (not-a-defect) loops + their entries.
    pruned_loop_ids = {1, 2}
    loops_after = [lp for lp in loops if lp["id"] not in pruned_loop_ids]
    triage_after = [t for t in triage if t["loop_id"] not in pruned_loop_ids]
    after = lens_tuning.attribute_by_source(loops_after, triage_after)["ship-review-r1"]
    assert after.rejection_ratio == 0.5  # 1 of 2 survives — noise under-reported
    assert after.rejection_ratio < full.rejection_ratio


def test_load_outcomes_tolerates_missing_stores(tmp_path):
    """No .goodfellow/ yet → empty outcomes, no crash, no dir created (read-only)."""
    loops, triage = lens_tuning.load_outcomes(project_root=str(tmp_path))
    assert loops == []
    assert triage == []
    assert not (tmp_path / ".goodfellow").exists()


# ---- data-honesty guards (round-2 findings) -------------------------------


def test_no_data_source_is_none_not_measured_zero():
    """F1/P65: a source with loops but zero surviving decisions must report N/A,
    not a measured 0% that reads as 'clean'."""
    loops = [_loop(1, "ship-review-r1"), _loop(2, "ship-review-r1")]
    stats = lens_tuning.attribute_by_source(loops, [])  # no triage records
    s = stats["ship-review-r1"]
    assert s.triaged == 0
    assert s.measured is False
    assert s.rejection_ratio is None
    assert s.override_ratio is None
    report = lens_tuning.render_report([], stats)
    assert "no-data" in report
    assert "N/A" in report
    j = json.loads(lens_tuning.render_report([], stats, as_json=True))
    assert j["sources"]["ship-review-r1"]["coverage"] == "no-data"
    assert j["sources"]["ship-review-r1"]["rejection_ratio"] is None


def test_unrecognized_decision_excluded_from_denominator():
    """F2/P33: a record with a decision outside the enum must not dilute the
    rejection denominator — it is counted as malformed, not triaged."""
    loops = [
        _loop(1, "ship-review-r1"),
        _loop(2, "ship-review-r1"),
        _loop(3, "ship-review-r1"),
    ]
    triage = [
        _triage(1, "not-a-defect"),
        {"loop_id": 2, "decision": "not-real"},  # bogus enum
        {"loop_id": 3, "decision": "??"},
    ]
    stats = lens_tuning.attribute_by_source(loops, triage)
    s = stats["ship-review-r1"]
    assert s.triaged == 1
    assert s.malformed == 2
    assert s.rejection_ratio == 1.0  # 1/1, not 1/3


def test_non_boolean_override_ignored_but_decision_counts():
    """F3/P33: operator_override counts only when strictly boolean True. A string
    'false'/'true' must NOT raise a disagreement signal — it is counted as
    invalid_override — but the record's valid decision still counts."""
    loops = [_loop(1, "ship-review-r1"), _loop(2, "ship-review-r1")]
    triage = [
        {"loop_id": 1, "decision": "not-a-defect", "operator_override": "false"},
        {"loop_id": 2, "decision": "not-a-defect", "operator_override": "true"},
    ]
    stats = lens_tuning.attribute_by_source(loops, triage)
    s = stats["ship-review-r1"]
    assert s.operator_override == 0
    assert s.invalid_override == 2
    assert s.triaged == 2
    assert s.not_a_defect == 2


def test_duplicate_loop_ids_are_quarantined_not_guessed():
    """F2/F3: colliding ids (documented Windows-concurrency corruption) must be
    quarantined — every colliding row excluded from attribution, and NO suggestion
    computed from the corrupted identity — not resolved by arbitrary array order."""
    loops = [
        _loop(1, "ship-review-r1"),
        _loop(1, "ship-review-r2"),  # same id, different source — ambiguous
        _loop(2, "ship-review-r3"),
    ]
    triage = [_triage(1, "not-a-defect"), _triage(2, "not-a-defect")]
    dups = lens_tuning.find_duplicate_loop_ids(loops)
    assert dups == {1}
    stats = lens_tuning.attribute_by_source(loops, triage)
    assert "ship-review-r1" not in stats
    assert "ship-review-r2" not in stats
    assert stats["ship-review-r3"].not_a_defect == 1
    report = lens_tuning.render_report([], stats, duplicate_ids=dups)
    assert "DATA-INTEGRITY WARNING" in report
    assert "QUARANTINED" in report


def test_join_prefers_durable_uuid_over_integer_id():
    """The loops.json<->triage-log join keys on the durable uuid, not loop_id: a
    record with the matching uuid but a mismatched integer id still attaches."""
    loops = [
        {
            "id": 1,
            "uuid": "aaaa",
            "title": "t",
            "source": "ship-review-r1",
            "status": "closed",
        }
    ]
    triage = [
        {
            "loop_id": 999,  # deliberately mismatched integer id
            "loop_uuid": "aaaa",
            "decision": "not-a-defect",
            "operator_override": False,
        }
    ]
    r1 = lens_tuning.attribute_by_source(loops, triage)["ship-review-r1"]
    assert r1.total == 1
    assert r1.triaged == 1
    assert r1.not_a_defect == 1


def test_reset_does_not_alias_triage_decision():
    """Cross-generation aliasing repro at the join: a historical triage record
    (uuid A, integer id 1) must NOT attach to a NEW loop that reused integer id 1
    after a loops.json reset (uuid B)."""
    new_loop = {
        "id": 1,
        "uuid": "bbbb",
        "title": "new",
        "source": "ship-review-r1",
        "status": "open",
    }
    historical = {
        "loop_id": 1,
        "loop_uuid": "aaaa",
        "decision": "not-a-defect",
        "operator_override": False,
    }
    r1 = lens_tuning.attribute_by_source([new_loop], [historical])["ship-review-r1"]
    assert r1.total == 1
    assert r1.triaged == 0  # historical decision NOT attributed to the new loop
    assert r1.not_a_defect == 0
    assert r1.rejection_ratio is None


def test_shared_integer_id_quarantined_even_with_distinct_uuids():
    """The operational mutation surface (CLI close/update, triage skill) still
    addresses loops by the integer id, so an integer-id collision stays a hazard
    and is quarantined even when the colliding rows carry distinct uuids — the
    join uses the uuid, but the operator warning must still fire."""
    loops = [
        {
            "id": 1,
            "uuid": "aaaa",
            "title": "a",
            "source": "ship-review-r1",
            "status": "closed",
        },
        {
            "id": 1,
            "uuid": "bbbb",
            "title": "b",
            "source": "ship-review-r2",
            "status": "closed",
        },
    ]
    triage = [
        {"loop_id": 1, "loop_uuid": "aaaa", "decision": "not-a-defect"},
        {"loop_id": 1, "loop_uuid": "bbbb", "decision": "real-defect"},
    ]
    assert lens_tuning.find_duplicate_loop_ids(loops) == {1}
    stats = lens_tuning.attribute_by_source(loops, triage)
    assert "ship-review-r1" not in stats
    assert "ship-review-r2" not in stats


def test_newer_legacy_record_beats_older_uuid_record():
    """Last-write-wins holds ACROSS the uuid/id namespaces: for a uuid-bearing
    loop, a newer uuid-less record must beat an older uuid-bearing one."""
    loop = {
        "id": 1,
        "uuid": "aaaa",
        "title": "t",
        "source": "ship-review-r1",
        "status": "open",
    }
    triage = [
        {"loop_id": 1, "loop_uuid": "aaaa", "decision": "real-defect"},  # older
        {"loop_id": 1, "decision": "not-a-defect"},  # newer, legacy-shaped
    ]
    r1 = lens_tuning.attribute_by_source([loop], triage)["ship-review-r1"]
    assert r1.real_defect == 0
    assert r1.not_a_defect == 1  # newer decision wins


def test_legacy_records_without_uuid_join_on_loop_id():
    """Backward compat: legacy loops + records with no uuid still join on loop_id."""
    loops = [
        {"id": 5, "title": "legacy", "source": "ship-review-r1", "status": "closed"}
    ]
    triage = [{"loop_id": 5, "decision": "not-a-defect"}]
    assert (
        lens_tuning.attribute_by_source(loops, triage)["ship-review-r1"].not_a_defect
        == 1
    )


def test_reset_aliasing_end_to_end_through_real_stores(tmp_path):
    """Full producer->consumer repro: preserve triage-log.jsonl across a loops.json
    reset and prove the surviving historical decision is not attributed to the new
    loop that reused the integer id."""
    root = str(tmp_path)
    lid = loop_store.add_loop("orig", source="ship-review-r1", project_root=root)
    orig_uuid = loop_store.get_loop(lid, project_root=root)["uuid"]
    triage_helper.log_decision(
        {"loop_id": lid, "loop_uuid": orig_uuid, "decision": "not-a-defect"},
        project_root=root,
    )
    # Reset loops.json only; the triage log survives.
    loop_store._loops_path(root).unlink()
    lid2 = loop_store.add_loop("reused-id", source="ship-review-r1", project_root=root)
    assert lid2 == lid == 1  # integer id collided across the reset
    assert loop_store.get_loop(lid2, project_root=root)["uuid"] != orig_uuid

    loops, triage = lens_tuning.load_outcomes(project_root=root)
    stats = lens_tuning.attribute_by_source(loops, triage)
    # The surviving historical not-a-defect must NOT attach to the new loop.
    assert stats["ship-review-r1"].total == 1
    assert stats["ship-review-r1"].triaged == 0


def test_cli_rejects_out_of_domain_gate_values():
    """F4/P33: reject min_sample < 1 and thresholds outside [0,1] / non-finite."""
    import pytest

    for argv in (
        ["--min-sample", "0"],
        ["--reject-threshold", "1.5"],
        ["--reject-threshold", "nan"],
        ["--override-threshold", "-0.1"],
    ):
        with pytest.raises(SystemExit):
            lens_tuning.main(argv)
