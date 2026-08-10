"""Strict validator for the judge decision table.

The judge (a second `codex exec` call) returns one *decision object* per
generator finding. This module validates that table STRICTLY: any violation
raises `JudgeContractError`; the caller (review_judge) then fails OPEN (keeps
the generator findings + a degradation banner). A corrupted judge block
therefore can never flip a Tier-2 into a ship-blocking halt nor silently drop
the finding set.

Decision object (the ONLY five fields):

    {
      "finding_id": "F1",
      "decision": "keep" | "drop",
      "judge_score": <int 0..10>,
      "drop_reason": <DROP_REASON | null>,
      "reclassified_to": "tier-3" | null,
      "causal_exception_valid": <bool | null>   # optional
    }

Rules enforced:
  - every generator finding_id has exactly one decision; no unknown ids; no dupes
  - decision in {keep, drop}
  - judge_score is an int in 0..10
  - drop_reason: null on keep; a valid enum member on drop
  - reclassified_to: non-null ONLY when drop_reason == "auto-zero-category"
  - causal_exception_valid: OPTIONAL; when present MUST be a real bool (reject
    "true"/"false" strings, reject int). The reconciler only reads it under the
    out-of-diff-boundary drop branch, so no cross-field enforcement here — a
    stray bool elsewhere is inert.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List


DROP_REASONS = frozenset(
    {"no-evidence", "below-threshold", "auto-zero-category", "out-of-diff-boundary"}
)
DECISIONS = frozenset({"keep", "drop"})


class JudgeContractError(ValueError):
    """Raised when the judge decision table violates the decision-table contract."""


def _is_int(value: Any) -> bool:
    # bool is a subclass of int; reject it — judge_score must be a real integer.
    return isinstance(value, int) and not isinstance(value, bool)


def validate_decisions(
    decisions: Any,
    generator_finding_ids: Iterable[str],
) -> List[Dict[str, Any]]:
    """Validate the judge decision list against the generator finding ids.

    Returns the decision list (unchanged) on success. Raises JudgeContractError
    on ANY violation — the caller fails open on that exception.
    """
    expected = list(generator_finding_ids)
    expected_set = set(expected)
    if len(expected_set) != len(expected):
        # Generator-side duplicate ids — reconciliation is ambiguous. Treat as a
        # contract violation → fail-open passthrough.
        raise JudgeContractError("duplicate finding_id in generator finding set")

    if not isinstance(decisions, list):
        raise JudgeContractError(
            f"decision table must be a list, got {type(decisions).__name__}"
        )

    seen: set[str] = set()
    for i, dec in enumerate(decisions):
        if not isinstance(dec, dict):
            raise JudgeContractError(f"decision[{i}] must be an object")

        fid = dec.get("finding_id")
        if not isinstance(fid, str) or not fid:
            raise JudgeContractError(f"decision[{i}] has invalid finding_id: {fid!r}")
        if fid not in expected_set:
            raise JudgeContractError(f"decision references unknown finding_id: {fid!r}")
        if fid in seen:
            raise JudgeContractError(f"duplicate decision for finding_id: {fid!r}")
        seen.add(fid)

        decision = dec.get("decision")
        if decision not in DECISIONS:
            raise JudgeContractError(f"decision[{fid}] invalid decision: {decision!r}")

        score = dec.get("judge_score")
        if not _is_int(score) or not (0 <= score <= 10):
            raise JudgeContractError(
                f"decision[{fid}] judge_score must be int 0..10, got {score!r}"
            )

        drop_reason = dec.get("drop_reason")
        reclassified_to = dec.get("reclassified_to")

        if decision == "keep":
            if drop_reason is not None:
                raise JudgeContractError(
                    f"decision[{fid}] keep must have null drop_reason"
                )
            if reclassified_to is not None:
                raise JudgeContractError(
                    f"decision[{fid}] keep must have null reclassified_to"
                )
        else:  # drop
            if drop_reason not in DROP_REASONS:
                raise JudgeContractError(
                    f"decision[{fid}] invalid drop_reason: {drop_reason!r}"
                )
            if reclassified_to is not None and drop_reason != "auto-zero-category":
                raise JudgeContractError(
                    f"decision[{fid}] reclassified_to set but drop_reason is {drop_reason!r}"
                )
            if reclassified_to is not None and reclassified_to != "tier-3":
                raise JudgeContractError(
                    f"decision[{fid}] invalid reclassified_to: {reclassified_to!r}"
                )

        # Optional field: exact-bool-or-null. Reject "true"/"false" strings and
        # ints (1/0) so only a real bool can satisfy the reconciler's `is True`
        # causal-exception gate; any other type is a contract violation → fail-open
        # passthrough (never silently defeats the scope gate).
        cev = dec.get("causal_exception_valid")
        if cev is not None and not isinstance(cev, bool):
            raise JudgeContractError(
                f"decision[{fid}] causal_exception_valid must be bool or null, "
                f"got {cev!r}"
            )

    missing = expected_set - seen
    if missing:
        raise JudgeContractError(
            f"missing decisions for finding_id(s): {sorted(missing)}"
        )

    return decisions
