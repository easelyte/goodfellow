"""Tests for triage_helper.py."""

import json
import tempfile
from pathlib import Path

import triage_helper


def test_both_agree_real():
    decision, confidence = triage_helper.reconcile("real-defect", "real-defect")
    assert decision == "real-defect"
    assert confidence == "high"


def test_both_agree_not():
    decision, confidence = triage_helper.reconcile("not-a-defect", "not-a-defect")
    assert decision == "not-a-defect"
    assert confidence == "high"


def test_one_opinion_one_unclear():
    decision, confidence = triage_helper.reconcile("real-defect", "still-unclear")
    assert decision == "real-defect"
    assert confidence == "medium"


def test_one_unclear_one_opinion():
    decision, confidence = triage_helper.reconcile("still-unclear", "not-a-defect")
    assert decision == "not-a-defect"
    assert confidence == "medium"


def test_disagree():
    decision, confidence = triage_helper.reconcile("real-defect", "not-a-defect")
    assert decision == "still-unclear"
    assert confidence == "low"


def test_both_unclear():
    decision, confidence = triage_helper.reconcile("still-unclear", "still-unclear")
    assert decision == "still-unclear"
    assert confidence == "low"


def test_invalid_input_treated_as_unclear():
    decision, confidence = triage_helper.reconcile("garbage", "real-defect")
    assert decision == "real-defect"
    assert confidence == "medium"


def test_must_decide_at_count_3():
    loop = {"triage_count": 3}
    assert triage_helper.is_must_decide(loop)


def test_not_must_decide_at_count_2():
    loop = {"triage_count": 2}
    assert not triage_helper.is_must_decide(loop)


def test_log_decision():
    with tempfile.TemporaryDirectory() as d:
        triage_helper.log_decision(
            {"loop_id": 1, "decision": "real-defect", "confidence": "high"},
            project_root=d,
        )
        log = triage_helper.read_triage_log(project_root=d)
        assert len(log) == 1
        assert log[0]["decision"] == "real-defect"


def test_log_survives_corrupt_line():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / ".shipline" / "triage-log.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text('{"good": true}\n{corrupt\n{"also": "good"}\n')
        log = triage_helper.read_triage_log(project_root=d)
        assert len(log) == 2
