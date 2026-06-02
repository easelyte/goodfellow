"""Tests for convergence.py and convergence_detector.py."""

import convergence
import convergence_detector


def test_zero_findings_round1_triggers_skepticism():
    result = convergence.check_round([], round_number=1)
    assert result.skepticism_triggered
    assert not result.converged


def test_zero_findings_round2_converges():
    result = convergence.check_round([], round_number=2)
    assert result.converged
    assert not result.skepticism_triggered


def test_zero_findings_round3_converges():
    result = convergence.check_round([], round_number=3)
    assert result.converged
    assert not result.skepticism_triggered


def test_safety_critical_is_ship_blocking():
    findings = [{"severity": "blocker", "defect_class": "security"}]
    result = convergence.check_round(findings, round_number=5)
    assert result.ship_blocking


def test_hard_cap_converges_at_cap():
    findings = [{"severity": "minor", "defect_class": "style"}]
    result = convergence.check_round(findings, round_number=6, hard_cap=6)
    assert result.converged
    assert len(result.deferred_findings) == 1


def test_hard_cap_converges_past_cap():
    findings = [{"severity": "minor", "defect_class": "style"}]
    result = convergence.check_round(findings, round_number=7, hard_cap=6)
    assert result.converged


def test_severity_drop_converges():
    round1 = [{"severity": "blocker", "defect_class": "auth"}]
    round2 = [{"severity": "minor", "defect_class": "style"}]
    result = convergence_detector.check_convergence([round1, round2])
    assert result.is_converged
    assert "dropped" in result.reason


def test_no_new_classes_converges():
    round1 = [{"severity": "major", "defect_class": "auth"}]
    round2 = [{"severity": "major", "defect_class": "auth"}]
    result = convergence_detector.check_convergence([round1, round2])
    assert result.is_converged


def test_new_classes_does_not_converge():
    round1 = [{"severity": "major", "defect_class": "auth"}]
    round2 = [
        {"severity": "major", "defect_class": "auth"},
        {"severity": "major", "defect_class": "perf"},
    ]
    result = convergence_detector.check_convergence([round1, round2])
    assert not result.is_converged


def test_knowledge_elevation():
    finding = {
        "severity": "minor",
        "area": "auth.py",
        "normalized_text": "missing check in `validate_token`",
    }
    gotchas = ["auth.py validate_token can return None on expired tokens"]
    elevated = convergence_detector.elevate_severity(finding, gotchas)
    assert elevated["severity"] == "major"
    assert elevated.get("knowledge_elevated")


def test_knowledge_elevation_capped_at_blocker():
    finding = {
        "severity": "major",
        "area": "auth.py",
        "normalized_text": "issue in `auth.py`",
    }
    gotchas = ["auth.py has known race condition"]
    elevated = convergence_detector.elevate_severity(finding, gotchas)
    assert elevated["severity"] == "blocker"


def test_empty_knowledge_noop():
    finding = {"severity": "minor", "area": "foo.py", "normalized_text": "test"}
    result = convergence_detector.elevate_severity(finding, [])
    assert result["severity"] == "minor"
    assert "knowledge_elevated" not in result


def test_new_safety_class_blocks_severity_drop_convergence():
    round1 = [{"severity": "blocker", "defect_class": "auth"}]
    round2 = [{"severity": "major", "defect_class": "injection"}]
    result = convergence_detector.check_convergence([round1, round2])
    assert not result.is_converged


def test_severity_drop_converges_when_new_class_not_safety():
    round1 = [{"severity": "blocker", "defect_class": "auth"}]
    round2 = [{"severity": "major", "defect_class": "style"}]
    result = convergence_detector.check_convergence([round1, round2])
    assert result.is_converged


def test_privilege_escalation_blocks_severity_drop_convergence():
    round1 = [{"severity": "blocker", "defect_class": "auth"}]
    round2 = [{"severity": "major", "defect_class": "privilege-escalation"}]
    result = convergence_detector.check_convergence([round1, round2])
    assert not result.is_converged


def test_single_round_needs_two():
    result = convergence_detector.check_convergence([[{"severity": "minor"}]])
    assert not result.is_converged
    assert "at least 2" in result.reason
