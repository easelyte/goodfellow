#!/usr/bin/env python3
"""Cross-round convergence detector.

Handles: convergence-by-class drift detection, knowledge-elevated severity,
verifier pass dispatch, research injection claim extraction.
Does NOT handle single-round budget/halt (see convergence.py).
"""

import json
import re
from dataclasses import dataclass, field


@dataclass
class ConvergenceResult:
    is_converged: bool = False
    rounds_run: int = 0
    findings_by_class: dict = field(default_factory=dict)
    severity_trend: str = ""
    reason: str = ""


@dataclass
class VerifiedFinding:
    finding_id: str = ""
    verdict: str = ""  # real, stale, noise
    reason: str = ""
    cited_line: str = ""


@dataclass
class Claim:
    text: str = ""
    source_section: str = ""
    claim_type: str = ""  # api-existence, version-compat, behavior, performance


def check_convergence(rounds_history):
    """Detect convergence across multiple rounds by tracking defect-class drift.

    rounds_history: list of lists, each inner list is findings for that round.
    """
    result = ConvergenceResult(rounds_run=len(rounds_history))

    if len(rounds_history) < 2:
        result.reason = "need at least 2 rounds"
        return result

    prev_classes = _classify_round(rounds_history[-2])
    curr_classes = _classify_round(rounds_history[-1])
    result.findings_by_class = curr_classes

    prev_max = _max_severity(prev_classes)
    curr_max = _max_severity(curr_classes)

    if curr_max == "none":
        result.is_converged = True
        result.severity_trend = "resolved"
        result.reason = "no findings in latest round"
        return result

    new_classes = set(curr_classes.keys()) - set(prev_classes.keys())

    if _severity_rank(curr_max) < _severity_rank(prev_max):
        new_safety = [
            c
            for c in new_classes
            if any(
                s in c
                for s in (
                    "security",
                    "data-loss",
                    "correctness",
                    "auth",
                    "injection",
                    "privilege",
                )
            )
        ]
        if not new_safety:
            result.is_converged = True
            result.severity_trend = f"{prev_max} -> {curr_max}"
            result.reason = f"severity dropped from {prev_max} to {curr_max}"
            return result
    if not new_classes and len(curr_classes) <= len(prev_classes):
        result.is_converged = True
        result.severity_trend = "stable"
        result.reason = "no new defect classes, count stable or declining"
        return result

    result.severity_trend = "active"
    result.reason = f"{len(new_classes)} new defect class(es)"
    return result


def elevate_severity(finding, gotchas):
    """Bump finding severity one tier if it matches a knowledge gotcha.

    Matching: finding's cited file path or symbol appears in a gotcha entry.
    Capped at blocker (safety-critical).
    """
    if not gotchas:
        return {**finding}

    area = finding.get("area", "")
    text = finding.get("normalized_text", "")
    search_terms = [area] + re.findall(r"`([^`]+)`", text)

    for gotcha in gotchas:
        for term in search_terms:
            if term and term in gotcha:
                severity = finding.get("severity", "minor")
                elevated = _elevate_one_tier(severity)
                finding = {
                    **finding,
                    "severity": elevated,
                    "knowledge_elevated": True,
                    "matched_gotcha": gotcha[:80],
                }
                return finding
    return finding


def run_verifier(findings, model="sonnet"):
    """Generate verifier prompts for each finding.

    Returns a list of dicts with the prompt and finding_id.
    The actual verification is done by the calling skill via Agent tool calls.
    """
    prompts = []
    for f in findings:
        finding_id = f.get("finding_id", f.get("short_label", "unknown"))
        area = f.get("area", "")
        text = f.get("normalized_text", "")
        prompt = (
            f"You are a finding verifier. Check if this review finding is still real "
            f"against the current code.\n\n"
            f"Finding: {text}\n"
            f"Cited area: {area}\n\n"
            f"Read the cited file(s). If the cited path no longer exists or the relevant "
            f"code moved, check surrounding files for the same pattern before classifying "
            f"as stale.\n\n"
            f"Respond with EXACTLY one JSON object:\n"
            f'{{"finding_id": {json.dumps(finding_id)}, "verdict": "real"|"stale"|"noise", '
            f'"reason": "<1-2 sentences>", "cited_line": "<file:line or null>"}}'
        )
        prompts.append({"finding_id": finding_id, "prompt": prompt, "model": model})
    return prompts


def extract_claims(findings):
    """Extract load-bearing factual claims from review findings for research injection."""
    claims = []
    patterns = [
        (
            r"(?:API|library|framework|tool|CLI)\s+\S+\s+(?:supports?|provides?|has)\s+",
            "api-existence",
        ),
        (r"version\s+\d+\.\d+", "version-compat"),
        (r"rate.?limit|quota|TTL|timeout", "performance"),
        (r"(?:available|works|runs)\s+on\s+", "platform-support"),
    ]
    for f in findings:
        text = f.get("normalized_text", "")
        for pattern, claim_type in patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            if matches:
                for sentence in text.split(". "):
                    if re.search(pattern, sentence, re.IGNORECASE):
                        claims.append(
                            Claim(
                                text=sentence.strip(),
                                source_section=f.get("area", ""),
                                claim_type=claim_type,
                            )
                        )
    return claims


SEVERITY_RANKS = {"none": 0, "minor": 1, "major": 2, "blocker": 3}


def _severity_rank(s):
    return SEVERITY_RANKS.get(s, 0)


def _max_severity(class_map):
    if not class_map:
        return "none"
    severities = [s for severities in class_map.values() for s in severities]
    if not severities:
        return "none"
    return max(severities, key=_severity_rank)


def _classify_round(findings):
    classes = {}
    for f in findings:
        dc = f.get("defect_class", "unclassified")
        sev = f.get("severity", "minor")
        classes.setdefault(dc, []).append(sev)
    return classes


def _elevate_one_tier(severity):
    tiers = ["minor", "major", "blocker"]
    idx = tiers.index(severity) if severity in tiers else 0
    return tiers[min(idx + 1, len(tiers) - 1)]
