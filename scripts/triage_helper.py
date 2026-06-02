#!/usr/bin/env python3
"""Shipline triage helper — reconciliation logic and JSONL ground truth logging."""

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path

TRIAGE_LOG = ".shipline/triage-log.jsonl"


def reconcile(reviewer_1, reviewer_2):
    """Apply the 4-case reconciliation table.

    Returns (decision, confidence).
    """
    r1 = reviewer_1.strip().lower() if reviewer_1 else "still-unclear"
    r2 = reviewer_2.strip().lower() if reviewer_2 else "still-unclear"

    valid = {"real-defect", "not-a-defect", "still-unclear"}
    r1 = r1 if r1 in valid else "still-unclear"
    r2 = r2 if r2 in valid else "still-unclear"

    if r1 == r2:
        if r1 == "still-unclear":
            return "still-unclear", "low"
        return r1, "high"

    if r1 == "still-unclear":
        return r2, "medium"
    if r2 == "still-unclear":
        return r1, "medium"

    return "still-unclear", "low"


def is_must_decide(loop):
    """Check if loop has hit the 3-cycle hard cap on unclear."""
    return loop.get("triage_count", 0) >= 3


def log_decision(decision_record, project_root="."):
    """Append a triage decision to the JSONL ground truth log.

    Uses file lock + flush + fsync per V5a.
    Truncated-final-line tolerance: reader skips lines that fail json.loads.
    """
    path = Path(project_root) / TRIAGE_LOG
    path.parent.mkdir(parents=True, exist_ok=True)

    record = {
        **decision_record,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    line = json.dumps(record, separators=(",", ":")) + "\n"

    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def read_triage_log(project_root="."):
    """Read triage log, skipping corrupt lines."""
    path = Path(project_root) / TRIAGE_LOG
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries
