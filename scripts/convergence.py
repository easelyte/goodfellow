#!/usr/bin/env python3
"""Single-round review decision helper.

Handles: round budget, early-stop skepticism, ship-blocking check,
deferred findings collection. Does NOT handle cross-round drift detection
(see convergence_detector.py — the two modules must not be merged).
"""

import json
from dataclasses import dataclass, field


@dataclass
class RoundResult:
    converged: bool = False
    ship_blocking: bool = False
    skepticism_triggered: bool = False
    deferred_findings: list = field(default_factory=list)
    reason: str = ""


SAFETY_CRITICAL_CLASSES = frozenset(
    [
        "security",
        "data-loss",
        "correctness",
        "auth-bypass",
        "privilege-escalation",
        "injection",
    ]
)


def check_round(round_findings, round_number, min_rounds=2, hard_cap=6):
    """Evaluate a single round's findings against budget and safety rules.

    Returns RoundResult with convergence/halt signals.
    """
    result = RoundResult()

    if round_number < 1:
        raise ValueError(f"round_number must be >= 1, got {round_number}")

    if round_number >= hard_cap:
        result.converged = True
        result.reason = f"hard cap ({hard_cap}) reached"
        result.deferred_findings = round_findings
        result.ship_blocking = _has_safety_critical(round_findings)
        return result

    if not round_findings and round_number < min_rounds:
        result.skepticism_triggered = True
        result.reason = f"zero findings at round {round_number} (below min_rounds={min_rounds}) — reviewer engagement check needed"
        return result

    if not round_findings and round_number >= min_rounds:
        result.converged = True
        result.reason = "no new findings"
        return result

    safety_critical = [f for f in round_findings if _is_safety_critical(f)]
    if safety_critical:
        result.ship_blocking = True
        result.reason = f"{len(safety_critical)} safety-critical finding(s) remain"

    return result


def _is_safety_critical(finding):
    severity = finding.get("severity", "").lower()
    defect_class = finding.get("defect_class", "").lower()
    if severity == "blocker":
        return True
    return any(cls in defect_class for cls in SAFETY_CRITICAL_CLASSES)


def _has_safety_critical(findings):
    return any(_is_safety_critical(f) for f in findings)
