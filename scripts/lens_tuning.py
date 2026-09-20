#!/usr/bin/env python3
"""Read-only reviewer-lens-tuning MVP.

Reviewer "lenses" live only as PROSE in the review skills and codex-bridge base
stances — no config, and no outcome record stores a lens tag. So this analyzer
joins the two DURABLE outcome stores — `.goodfellow/loops.json` and
`.goodfellow/triage-log.jsonl` — and surfaces review `source`s (e.g.
``ship-review-r2``) whose surviving deferred findings were mostly triaged
not-a-defect, or mostly operator-overridden. It is a HUMAN-ATTENTION POINTER, not
a lens error rate; a human validates and edits the lens prose.

The metric is deliberately narrow because the underlying data is a biased,
erodable subsample — the report states each limit, and the tests enforce it:

- **Deferred-only denominator.** ship files loops only for findings DEFERRED at
  the review's convergence exit (and polish-tier deferred findings become gotchas,
  not loops). Findings fixed inline during the review never enter either store. So
  the ratio is a *deferred-loop rejection rate*, NOT the lens's false-positive
  rate — do not weaken a lens on this signal alone.
- **Retention floor.** A not-a-defect decision CLOSES its loop; retention
  (GOODFELLOW_TRIAGE_RETENTION_DAYS, default 90d) prunes old closed-loop triage
  entries while active real-defect loops persist. So rejection counts are a FLOOR,
  biased toward UNDER-reporting rejection; only surviving records are shown.
- **Override direction.** operator_override is a direction-less boolean — it flags
  reviewer/operator disagreement, not proven noise.
- Source-granularity attribution stays (a review runs multiple lenses; the
  `source` breadcrumb names all of them). The judge also tags
  each finding with a `lens`, threaded onto the loop, so this analyzer ALSO
  attributes per-lens — but only for loops filed after the tag shipped; older
  loops have no lens and are bucketed as `unattributed` (never flagged to tune).
- Over-firing only: under-firing — real defects a lens MISSED — is not measurable
  here and is out of scope. The judge-audit sidecar is ephemeral /tmp, not joined.

Data-honesty guards: a source with no surviving decisions reports N/A, not a
measured 0% (no-data != measured-zero); a triage record with an unrecognized
`decision` is excluded and counted as `malformed`; a non-boolean
`operator_override` is ignored (never a disagreement signal) and counted as
`invalid_override`, while the record's valid decision still counts; loop ids that
collide across rows (the documented Windows-concurrency corruption in loop_store)
are QUARANTINED — every colliding row is excluded from attribution and surfaced,
so a corrupted identity never yields a suggestion.

Cross-generation identity: the join keys on the DURABLE loop identity — the
immutable `uuid` minted per loop by loop_store — rather than the per-store
integer `loop_id`. If `loops.json` is reset or restored while `triage-log.jsonl`
survives, a new loop reusing an old integer id carries a fresh uuid, so it no
longer inherits the historical decision recorded against the old incarnation.
Residual (narrow): a legacy loop that predates the uuid field only gains one on
the store's next WRITE (loop_store backfills lazily under the lock). So a legacy
loop that is never mutated after upgrade and is triaged without a store write can
still alias across a reset. Any loop that has been added/closed/re-triaged since
the upgrade carries a uuid, and its triage record is collision-proof.

Read-only and side-effect-free: it never edits lens prose or writes state.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import re
import sys
from dataclasses import asdict, dataclass, field

_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import loop_store  # noqa: E402
import triage_helper  # noqa: E402
from judge_decision_validator import (  # noqa: E402
    LENS_UNATTRIBUTED,
    normalize_lens,
)

# LENS_UNATTRIBUTED (imported from the validator so there is ONE definition) is
# the no-provenance bucket: loops filed before the lens tag shipped ,
# by hand, or with a malformed/unrecognized lens. It is surfaced so the report is
# honest about how much history predates the tag, but is NEVER flagged as a lens
# to tune. It is DISTINCT from the explicit "other" lens, which IS measured data
# (P65: no-data must not masquerade as a measured-zero "other").

# Every named lens lives in the SAME generator-stance block, so a per-lens
# pointer names that one location rather than a per-lens prose map (the whole
# point of the tag: the lens IS the category, no source->prose indirection).
LENS_PROSE_LOCATION = (
    "scripts/codex-bridge.sh build_generator_prompt <attack_surface> + the "
    "per-KIND base stance (~L238-312); tune the framing for this attack surface."
)

DECISION_NOISE = "not-a-defect"
DECISION_REAL = "real-defect"
DECISION_UNCLEAR = "still-unclear"
VALID_DECISIONS = frozenset({DECISION_NOISE, DECISION_REAL, DECISION_UNCLEAR})

SIGNAL_REJECTION = "deferred-rejection"
SIGNAL_OVERRIDE = "operator-disagreement"

# Ordered source-breadcrumb -> lens-prose location. The breadcrumb is the only
# durable link from an outcome back toward the prose a human would tune.
SOURCE_LENS_MAP: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"^ship-review"),
        "scripts/codex-bridge.sh build_generator_prompt code/diff stance (~L238-264) "
        "+ the ship operator lens passed to the review",
    ),
    (
        re.compile(r"^/?codex-review"),
        "skills/codex-review/SKILL.md + scripts/codex-bridge.sh per-KIND base stance",
    ),
    (
        re.compile(r"^spec-review"),
        "skills/spec-review/SKILL.md reviewer lens prompts (~L62-74)",
    ),
    (
        re.compile(r"^plan-review"),
        "skills/plan-review/SKILL.md reviewer lens prompts (~L93-103)",
    ),
]

CAVEATS = [
    "Denominator is DEFERRED findings only: ship files loops for findings deferred at "
    "convergence (polish-tier deferred findings become gotchas, not loops), and findings "
    "fixed inline during review never enter the stores. So this is a deferred-loop "
    "rejection rate, NOT the lens false-positive rate — do not weaken a lens on it alone.",
    "Rejection counts are a FLOOR: not-a-defect closes a loop, and retention "
    "(GOODFELLOW_TRIAGE_RETENTION_DAYS, default 90d) prunes old closed-loop triage entries "
    "while active real-defect loops persist — so pruned rejections are under-reported. "
    "Only surviving records are shown.",
    "operator_override is a direction-less boolean: it flags reviewer/operator "
    "disagreement on a source's findings, not proven noise. A non-boolean value is "
    "ignored (never a disagreement signal) and counted as invalid_override.",
    "The join keys on the durable loop uuid (minted per loop by loop_store), not the "
    "per-store integer loop_id. After a loops.json reset/restore while triage-log.jsonl "
    "survives, a new loop reusing an old integer id carries a fresh uuid and no longer "
    "inherits the historical decision. Residual: legacy loops gain a uuid only on the "
    "store's next write (loop_store backfills lazily under the lock), so a legacy loop "
    "never mutated after upgrade and triaged without a store write can still alias across "
    "a reset; any loop touched since upgrade is collision-proof.",
    "A source with no surviving triage decisions is reported as N/A (no data), never as a "
    "measured 0% — absent evidence is not a clean result.",
    "Source-granularity attribution names every lens a review runs (the `source` "
    "breadcrumb). The judge tags each finding with a `lens` threaded onto "
    "the loop, so per-lens attribution is ALSO available — but only for loops filed after "
    "the tag shipped; older/hand-filed loops have no lens and are bucketed as "
    "`unattributed` and never flagged as a lens to tune.",
    "Over-firing signal only. Under-firing — real defects a lens MISSED — is not "
    "measurable from these stores and is out of scope. The judge-audit sidecar is "
    "ephemeral /tmp and is not joined.",
    "This tool only SUGGESTS which lens prose to revisit. A human validates and edits.",
]


def _lens_location(source: str) -> str | None:
    for pat, loc in SOURCE_LENS_MAP:
        if pat.search(source):
            return loc
    return None


@dataclass
class SourceStats:
    source: str
    total: int = 0
    triaged: int = 0
    real_defect: int = 0
    not_a_defect: int = 0
    still_unclear: int = 0
    operator_override: int = 0
    malformed: int = 0  # records for this source with an unrecognized decision
    invalid_override: int = 0  # records whose operator_override was not a boolean

    @property
    def measured(self) -> bool:
        return self.triaged > 0

    @property
    def rejection_ratio(self) -> float | None:
        """Fraction of SURVIVING triaged deferred loops triaged not-a-defect, or
        None when nothing was measured (no-data != measured-zero). A floor (see
        module docstring), not a lens false-positive rate."""
        return self.not_a_defect / self.triaged if self.triaged else None

    @property
    def override_ratio(self) -> float | None:
        """Fraction of triaged loops whose reconciled decision the operator overrode
        (direction not recorded), or None when nothing was measured."""
        return self.operator_override / self.triaged if self.triaged else None


@dataclass
class Suggestion:
    source: str
    lens_location: str | None
    signals: list[str] = field(default_factory=list)
    rejection_ratio: float | None = None
    override_ratio: float | None = None
    triaged: int = 0
    not_a_defect: int = 0
    operator_override: int = 0
    message: str = ""


def load_outcomes(project_root: str = ".") -> tuple[list[dict], list[dict]]:
    """Read the two durable outcome stores. Side-effect-free: reads only, never
    creates ``.goodfellow/`` (both underlying readers return empty on absence)."""
    loops = loop_store.list_loops(project_root=project_root)
    triage = triage_helper.read_triage_log(project_root=project_root)
    return loops, triage


def find_duplicate_loop_ids(loops: list[dict]) -> set:
    """Integer loop ids that appear on more than one loop row. loop_store
    documents that concurrent Windows writers can mint colliding integer ids, and
    the operational mutation surface (CLI ``close``/``update-triage``, triage
    skill) still addresses loops by that integer id — so a collision is a genuine
    hazard (a decision, close, or update could hit the wrong loop). Callers
    quarantine every colliding row from attribution and surface the warning.

    Deliberately keyed on the raw integer id, NOT the durable uuid: even when the
    colliding rows carry distinct uuids, the id-addressed mutation surface remains
    ambiguous, so the operator warning must still fire."""
    seen: set = set()
    dups: set = set()
    for loop in loops:
        lid = loop.get("id")
        if lid is None:
            continue
        if lid in seen:
            dups.add(lid)
        seen.add(lid)
    return dups


def attribute_by_source(
    loops: list[dict], triage: list[dict]
) -> dict[str, SourceStats]:
    """Join loops to their latest triage decision and bucket by `source`.

    - Loops without a `source` are skipped (no lens proxy).
    - Loop ids that collide across rows are QUARANTINED — every colliding row is
      excluded from attribution (identity is corrupted, so a decision cannot be
      trusted against any of them). find_duplicate_loop_ids surfaces the warning.
    - A re-triaged loop uses its most recent decision (append-only: last wins).
    - A record with a decision outside VALID_DECISIONS is NOT counted in `triaged`;
      it increments `malformed`. operator_override counts only when strictly the
      boolean True; any other non-null value is ignored and counted as
      `invalid_override`, while the record's valid decision still counts.
    """
    # Two indices so post-fix records are collision-proof while legacy records
    # still attach. A record that CARRIES a loop_uuid is reachable ONLY by that
    # uuid — never by its integer loop_id — so a new loop that reused an old id
    # after a reset cannot inherit it. A record lacking loop_uuid (written before
    # uuids existed) is keyed by loop_id, the legacy path. Each entry stores its
    # append ordinal so last-write-wins holds ACROSS the two namespaces (a newer
    # legacy record must beat an older uuid record for the same loop).
    by_uuid: dict[object, tuple[int, dict]] = {}
    by_id: dict[object, tuple[int, dict]] = {}
    for i, rec in enumerate(triage):
        u = rec.get("loop_uuid")
        if u:
            by_uuid[u] = (i, rec)
        else:
            lid = rec.get("loop_id")
            if lid is not None:
                by_id[lid] = (i, rec)

    quarantined = find_duplicate_loop_ids(loops)
    stats: dict[str, SourceStats] = {}
    for loop in loops:
        source = loop.get("source")
        if not source:
            continue
        if loop.get("id") in quarantined:
            continue  # ambiguous integer id — attribute to nothing (never guess a source)
        s = stats.setdefault(source, SourceStats(source=source))
        s.total += 1
        # Candidate decisions: the loop's durable uuid match and its legacy
        # integer-id match. Pick whichever was appended LAST (true last-write-wins).
        candidates = []
        u = loop.get("uuid")
        if u and u in by_uuid:
            candidates.append(by_uuid[u])
        id_match = by_id.get(loop.get("id"))
        if id_match is not None:
            candidates.append(id_match)
        rec = max(candidates, key=lambda t: t[0])[1] if candidates else None
        if rec is None:
            continue
        dec = rec.get("decision")
        if dec not in VALID_DECISIONS:
            s.malformed += 1
            continue  # unrecognized decision must not dilute the denominator
        s.triaged += 1
        if dec == DECISION_NOISE:
            s.not_a_defect += 1
        elif dec == DECISION_REAL:
            s.real_defect += 1
        elif dec == DECISION_UNCLEAR:
            s.still_unclear += 1
        ov = rec.get("operator_override")
        if ov is True:
            s.operator_override += 1
        elif ov is not None and not isinstance(ov, bool):
            s.invalid_override += 1  # present but not a boolean — ignored as a signal
    return stats


def suggest_lens_tweaks(
    stats: dict[str, SourceStats],
    *,
    min_sample: int = 3,
    reject_threshold: float = 0.5,
    override_threshold: float = 0.5,
) -> list[Suggestion]:
    """Flag a source (min_sample-gated) when EITHER its deferred-rejection rate OR
    its operator-override rate clears its threshold. Ranked by combined impact.
    Overrides are a first-class gating signal, not cosmetic."""
    out: list[Suggestion] = []
    for source, s in stats.items():
        if s.triaged < min_sample:
            continue
        rr = s.rejection_ratio
        orr = s.override_ratio
        signals: list[str] = []
        if rr is not None and rr >= reject_threshold:
            signals.append(SIGNAL_REJECTION)
        if orr is not None and orr >= override_threshold:
            signals.append(SIGNAL_OVERRIDE)
        if not signals:
            continue
        loc = _lens_location(source)
        where = (
            f"Revisit the lens prose at: {loc}."
            if loc
            else (
                "No known source→lens mapping — locate the review path that emits "
                f"source '{source}' and revisit its lens prose."
            )
        )
        parts: list[str] = []
        if SIGNAL_REJECTION in signals:
            parts.append(
                f"{s.not_a_defect}/{s.triaged} surviving deferred findings triaged "
                f"not-a-defect ({round(rr * 100)}%)"
            )
        if SIGNAL_OVERRIDE in signals:
            parts.append(
                f"{s.operator_override}/{s.triaged} operator-overridden ({round(orr * 100)}%)"
            )
        message = (
            f"'{source}': {', '.join(parts)}. This is deferred-loop disposition, NOT a "
            f"lens error rate — investigate against the full review history before tuning. "
            + where
        )
        out.append(
            Suggestion(
                source=source,
                lens_location=loc,
                signals=signals,
                rejection_ratio=rr,
                override_ratio=orr,
                triaged=s.triaged,
                not_a_defect=s.not_a_defect,
                operator_override=s.operator_override,
                message=message,
            )
        )
    out.sort(
        key=lambda x: (
            max(x.rejection_ratio or 0.0, x.override_ratio or 0.0) * x.triaged
        ),
        reverse=True,
    )
    return out


# --------------------------------------------------------------------------- #
# Per-lens attribution
# --------------------------------------------------------------------------- #
@dataclass
class LensStats:
    """Same shape as SourceStats, bucketed by the finding's judge-assigned lens
    instead of the review source breadcrumb."""

    lens: str
    total: int = 0
    triaged: int = 0
    real_defect: int = 0
    not_a_defect: int = 0
    still_unclear: int = 0
    operator_override: int = 0
    malformed: int = 0
    invalid_override: int = 0

    @property
    def measured(self) -> bool:
        return self.triaged > 0

    @property
    def rejection_ratio(self) -> float | None:
        return self.not_a_defect / self.triaged if self.triaged else None

    @property
    def override_ratio(self) -> float | None:
        return self.operator_override / self.triaged if self.triaged else None


@dataclass
class LensSuggestion:
    lens: str
    lens_location: str | None
    signals: list[str] = field(default_factory=list)
    rejection_ratio: float | None = None
    override_ratio: float | None = None
    triaged: int = 0
    not_a_defect: int = 0
    operator_override: int = 0
    message: str = ""


def _normalized_loop_lens(loop: dict) -> str:
    """The loop's lens bucket, via the same normalize_lens() the judge path uses.
    A recognized member (incl explicit "other") is kept as measured data; a
    missing/None/blank/unrecognized lens -> LENS_UNATTRIBUTED. Never raises."""
    return normalize_lens(loop.get("lens"))


def attribute_by_lens(loops: list[dict], triage: list[dict]) -> dict[str, LensStats]:
    """Join loops to their latest triage decision and bucket by the loop's lens.

    Uses the SAME durable-uuid join as attribute_by_source (uuid-keyed records are
    collision-proof across a store reset; legacy records without loop_uuid key on
    the integer loop_id; last-write-wins across both namespaces). Colliding integer
    ids are QUARANTINED, a decision outside VALID_DECISIONS increments `malformed`
    and is not counted in `triaged`, and operator_override counts only strict True.
    Loops with no lens are bucketed as LENS_UNATTRIBUTED (surfaced, never flagged).
    """
    by_uuid: dict[object, tuple[int, dict]] = {}
    by_id: dict[object, tuple[int, dict]] = {}
    for i, rec in enumerate(triage):
        u = rec.get("loop_uuid")
        if u:
            by_uuid[u] = (i, rec)
        else:
            lid = rec.get("loop_id")
            if lid is not None:
                by_id[lid] = (i, rec)

    quarantined = find_duplicate_loop_ids(loops)
    stats: dict[str, LensStats] = {}
    for loop in loops:
        if loop.get("id") in quarantined:
            continue  # corrupted identity — attribute to nothing
        lens = _normalized_loop_lens(loop)
        s = stats.setdefault(lens, LensStats(lens=lens))
        s.total += 1
        candidates = []
        u = loop.get("uuid")
        if u and u in by_uuid:
            candidates.append(by_uuid[u])
        id_match = by_id.get(loop.get("id"))
        if id_match is not None:
            candidates.append(id_match)
        rec = max(candidates, key=lambda t: t[0])[1] if candidates else None
        if rec is None:
            continue
        dec = rec.get("decision")
        if dec not in VALID_DECISIONS:
            s.malformed += 1
            continue
        s.triaged += 1
        if dec == DECISION_NOISE:
            s.not_a_defect += 1
        elif dec == DECISION_REAL:
            s.real_defect += 1
        elif dec == DECISION_UNCLEAR:
            s.still_unclear += 1
        ov = rec.get("operator_override")
        if ov is True:
            s.operator_override += 1
        elif ov is not None and not isinstance(ov, bool):
            s.invalid_override += 1
    return stats


def suggest_lens_tweaks_by_lens(
    stats: dict[str, LensStats],
    *,
    min_sample: int = 3,
    reject_threshold: float = 0.5,
    override_threshold: float = 0.5,
) -> list[LensSuggestion]:
    """Flag a lens (min_sample-gated) when EITHER its deferred-rejection rate OR
    its operator-override rate clears its threshold. LENS_UNATTRIBUTED is never
    flagged — it is not a real lens, just pre-tag/hand-filed history."""
    out: list[LensSuggestion] = []
    for lens, s in stats.items():
        if lens == LENS_UNATTRIBUTED:
            continue
        if s.triaged < min_sample:
            continue
        rr = s.rejection_ratio
        orr = s.override_ratio
        signals: list[str] = []
        if rr is not None and rr >= reject_threshold:
            signals.append(SIGNAL_REJECTION)
        if orr is not None and orr >= override_threshold:
            signals.append(SIGNAL_OVERRIDE)
        if not signals:
            continue
        parts: list[str] = []
        if SIGNAL_REJECTION in signals:
            parts.append(
                f"{s.not_a_defect}/{s.triaged} surviving deferred findings triaged "
                f"not-a-defect ({round(rr * 100)}%)"
            )
        if SIGNAL_OVERRIDE in signals:
            parts.append(
                f"{s.operator_override}/{s.triaged} operator-overridden ({round(orr * 100)}%)"
            )
        message = (
            f"lens '{lens}': {', '.join(parts)}. This is deferred-loop disposition, NOT a "
            f"lens error rate — investigate against the full review history before tuning. "
            f"Revisit the lens prose at: {LENS_PROSE_LOCATION}"
        )
        out.append(
            LensSuggestion(
                lens=lens,
                lens_location=LENS_PROSE_LOCATION,
                signals=signals,
                rejection_ratio=rr,
                override_ratio=orr,
                triaged=s.triaged,
                not_a_defect=s.not_a_defect,
                operator_override=s.operator_override,
                message=message,
            )
        )
    out.sort(
        key=lambda x: (
            max(x.rejection_ratio or 0.0, x.override_ratio or 0.0) * x.triaged
        ),
        reverse=True,
    )
    return out


def _pct(ratio: float | None) -> str:
    return "N/A" if ratio is None else f"{round(ratio * 100)}%"


def render_report(
    suggestions: list[Suggestion],
    stats: dict[str, SourceStats],
    *,
    duplicate_ids: set | None = None,
    as_json: bool = False,
    lens_stats: dict[str, "LensStats"] | None = None,
    lens_suggestions: list["LensSuggestion"] | None = None,
) -> str:
    dups = sorted(duplicate_ids, key=str) if duplicate_ids else []
    if as_json:
        return json.dumps(
            {
                "suggestions": [asdict(s) for s in suggestions],
                "sources": {
                    src: {
                        "total": s.total,
                        "triaged": s.triaged,
                        "real_defect": s.real_defect,
                        "not_a_defect": s.not_a_defect,
                        "still_unclear": s.still_unclear,
                        "operator_override": s.operator_override,
                        "malformed": s.malformed,
                        "invalid_override": s.invalid_override,
                        "coverage": "measured" if s.measured else "no-data",
                        "rejection_ratio": (
                            None
                            if s.rejection_ratio is None
                            else round(s.rejection_ratio, 4)
                        ),
                        "override_ratio": (
                            None
                            if s.override_ratio is None
                            else round(s.override_ratio, 4)
                        ),
                    }
                    for src, s in stats.items()
                },
                "lenses": {
                    lens: {
                        "total": ls.total,
                        "triaged": ls.triaged,
                        "real_defect": ls.real_defect,
                        "not_a_defect": ls.not_a_defect,
                        "still_unclear": ls.still_unclear,
                        "operator_override": ls.operator_override,
                        "malformed": ls.malformed,
                        "invalid_override": ls.invalid_override,
                        "coverage": "measured" if ls.measured else "no-data",
                        "rejection_ratio": (
                            None
                            if ls.rejection_ratio is None
                            else round(ls.rejection_ratio, 4)
                        ),
                        "override_ratio": (
                            None
                            if ls.override_ratio is None
                            else round(ls.override_ratio, 4)
                        ),
                    }
                    for lens, ls in (lens_stats or {}).items()
                },
                "lens_suggestions": [asdict(s) for s in (lens_suggestions or [])],
                "duplicate_loop_ids": dups,
                "caveats": CAVEATS,
            },
            indent=2,
        )

    lines: list[str] = []
    lines.append("# Reviewer-lens tuning (read-only, human-attention pointer)")
    lines.append("")
    if dups:
        lines.append(
            f"> DATA-INTEGRITY WARNING: duplicate loop ids {dups} — QUARANTINED (every "
            "colliding row excluded from attribution). Investigate concurrent writes to "
            "loops.json."
        )
        lines.append("")
    if suggestions:
        lines.append(f"## Sources to review ({len(suggestions)})")
        for i, s in enumerate(suggestions, 1):
            lines.append(f"{i}. [{', '.join(s.signals)}] {s.message}")
    else:
        lines.append("## Sources to review (0)")
        lines.append("No source cleared the sample + signal gates. Nothing flagged.")
    lines.append("")
    lines.append("## Per-source outcomes (surviving deferred loops only)")
    if stats:
        lines.append(
            "source | total | triaged | real | not-a-defect | override | malformed | "
            "invalid-override | coverage | reject% | override%"
        )
        lines.append("--- | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---")
        for src, s in sorted(
            stats.items(), key=lambda kv: kv[1].rejection_ratio or -1.0, reverse=True
        ):
            coverage = "measured" if s.measured else "no-data"
            lines.append(
                f"{src} | {s.total} | {s.triaged} | {s.real_defect} | {s.not_a_defect} | "
                f"{s.operator_override} | {s.malformed} | {s.invalid_override} | {coverage} | "
                f"{_pct(s.rejection_ratio)} | {_pct(s.override_ratio)}"
            )
    else:
        lines.append("No review-sourced loops found.")
    if lens_stats is not None:
        lines.append("")
        lines.append("## Per-lens attribution ")
        ls_sugg = lens_suggestions or []
        if ls_sugg:
            lines.append(f"### Lenses to review ({len(ls_sugg)})")
            for i, s in enumerate(ls_sugg, 1):
                lines.append(f"{i}. [{', '.join(s.signals)}] {s.message}")
        else:
            lines.append("### Lenses to review (0)")
            lines.append("No lens cleared the sample + signal gates. Nothing flagged.")
        lines.append("")
        lines.append("### Per-lens outcomes (surviving deferred loops only)")
        if lens_stats:
            lines.append(
                "lens | total | triaged | real | not-a-defect | override | malformed | "
                "invalid-override | coverage | reject% | override%"
            )
            lines.append(
                "--- | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---"
            )
            for lens, ls in sorted(
                lens_stats.items(),
                key=lambda kv: kv[1].rejection_ratio or -1.0,
                reverse=True,
            ):
                coverage = "measured" if ls.measured else "no-data"
                lines.append(
                    f"{lens} | {ls.total} | {ls.triaged} | {ls.real_defect} | "
                    f"{ls.not_a_defect} | {ls.operator_override} | {ls.malformed} | "
                    f"{ls.invalid_override} | {coverage} | {_pct(ls.rejection_ratio)} | "
                    f"{_pct(ls.override_ratio)}"
                )
        else:
            lines.append(
                "No lens-tagged loops found (pre-lens-tag history has no lens)."
            )
    lines.append("")
    lines.append("## Caveats (read these before acting)")
    for c in CAVEATS:
        lines.append(f"- {c}")
    return "\n".join(lines)


def _positive_int(value: str) -> int:
    ivalue = int(value)
    if ivalue < 1:
        raise argparse.ArgumentTypeError("must be an integer >= 1")
    return ivalue


def _unit_float(value: str) -> float:
    fvalue = float(value)
    if not math.isfinite(fvalue) or not (0.0 <= fvalue <= 1.0):
        raise argparse.ArgumentTypeError("must be a finite fraction in [0, 1]")
    return fvalue


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Read-only reviewer-lens tuning suggestions."
    )
    p.add_argument("--root", default=".", help="Project root containing .goodfellow/")
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    p.add_argument(
        "--min-sample",
        type=_positive_int,
        default=3,
        help="Min triaged findings to flag (>=1)",
    )
    p.add_argument(
        "--reject-threshold",
        type=_unit_float,
        default=0.5,
        help="Min surviving not-a-defect fraction to flag a source (0..1)",
    )
    p.add_argument(
        "--override-threshold",
        type=_unit_float,
        default=0.5,
        help="Min operator-override fraction to flag a source (0..1)",
    )
    args = p.parse_args(argv)

    loops, triage = load_outcomes(project_root=args.root)
    stats = attribute_by_source(loops, triage)
    duplicate_ids = find_duplicate_loop_ids(loops)
    suggestions = suggest_lens_tweaks(
        stats,
        min_sample=args.min_sample,
        reject_threshold=args.reject_threshold,
        override_threshold=args.override_threshold,
    )
    lens_stats = attribute_by_lens(loops, triage)
    lens_suggestions = suggest_lens_tweaks_by_lens(
        lens_stats,
        min_sample=args.min_sample,
        reject_threshold=args.reject_threshold,
        override_threshold=args.override_threshold,
    )
    print(
        render_report(
            suggestions,
            stats,
            duplicate_ids=duplicate_ids,
            as_json=args.json,
            lens_stats=lens_stats,
            lens_suggestions=lens_suggestions,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
