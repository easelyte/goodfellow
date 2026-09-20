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
  - lens: OPTIONAL ; the interpretation frame the finding falls under.
    FAIL-OPEN — validate_decisions NEVER raises on it; review_judge coerces any
    value via normalize_lens() (recognized member incl explicit "other" kept as
    measured data; absent/malformed/unrecognized -> LENS_UNATTRIBUTED, never
    "other"), so a bad lens degrades attribution instead of failing the pass.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List


DROP_REASONS = frozenset(
    {"no-evidence", "below-threshold", "auto-zero-category", "out-of-diff-boundary"}
)
DECISIONS = frozenset({"keep", "drop"})

# --- Reviewer-lens vocabulary ----------------------------------
# The judge tags each finding with the LENS (interpretation frame) it falls
# under. Lenses previously lived only as PROSE in the generator stances with no
# durable tag, so per-lens outcome attribution was impossible; this tag dissolves
# that. The set is cross-KIND (code/spec/plan) — the primary value is code review.
#
# FAIL-OPEN CONTRACT: `lens` is an OPTIONAL field on a decision object. It NEVER
# raises JudgeContractError — an absent, malformed, non-string, or unrecognized
# value is normalized to LENS_UNATTRIBUTED by normalize_lens(). A bad lens
# therefore can never flip the whole judge pass to unjudged passthrough (that
# escalation is reserved for a violation of the core keep/drop grounding fields).
#
# MEASURED-ZERO vs NO-DATA (P65): `LENS_UNKNOWN` ("other") is a VALID vocabulary
# member — the judge's explicit "fits no named lens" choice, which IS measured
# data and stays eligible for lens-tuning. It is DISTINCT from LENS_UNATTRIBUTED
# ("no lens provenance at all": absent/malformed/unrecognized). Collapsing the two
# would let a batch of no-provenance findings masquerade as a measured "other"
# lens and emit a false tuning signal, so normalize_lens keeps them separate.
LENS_UNKNOWN = "other"  # explicit valid judge choice: fits no named lens
LENS_UNATTRIBUTED = "unattributed"  # no usable lens provenance (no-data, not a lens)
LENS_VOCABULARY = frozenset(
    {
        "auth-trust",  # auth, permissions, tenant isolation, trust boundaries
        "data-integrity",  # data loss, corruption, duplication, irreversible state
        "failure-handling",  # rollback, retries, partial failure, idempotency
        "concurrency",  # races, ordering, stale state, re-entrancy
        "input-edge",  # empty/null/timeout/degraded-dependency, edge inputs
        "compat-migration",  # version skew, schema drift, migration, compatibility
        "observability",  # observability / audit gaps
        "contract-scope",  # contradictions, ambiguity, scope gaps, task ordering
        LENS_UNKNOWN,  # explicit "fits no named lens" — a valid, measurable choice
    }
)


def normalize_lens(value: Any) -> str:
    """Coerce a judge-supplied lens value to a canonical vocabulary member, or to
    LENS_UNATTRIBUTED when there is no usable lens provenance.

    Fail-open, and P65-preserving: a recognized vocabulary member (INCLUDING the
    explicit "other" = LENS_UNKNOWN) is returned as-is (measured data); an absent,
    non-string, blank, or unrecognized value returns LENS_UNATTRIBUTED (no-data) —
    NOT "other", so missing provenance never contaminates the valid "other" bucket.
    Never raises.
    """
    if not isinstance(value, str):
        return LENS_UNATTRIBUTED
    v = value.strip().lower()
    if not v:
        return LENS_UNATTRIBUTED
    return v if v in LENS_VOCABULARY else LENS_UNATTRIBUTED


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
