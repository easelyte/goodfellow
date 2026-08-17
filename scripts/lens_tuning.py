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
- Attribution is at `source` granularity, NOT per-lens (a review runs multiple
  lenses; nothing ties a finding to one).
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

Known gap (deferred, see caveats): the join keys on `loop_id` across two
independently-durable stores. If `loops.json` is reset or restored while
`triage-log.jsonl` survives, new loops reuse old ids and can inherit unrelated
historical decisions — there is no cross-generation identity today. The durable
fix (an immutable loop UUID / store epoch on both stores) is a producer-side
change beyond this read-only MVP and is tracked as a follow-up.

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
    "The join keys on loop_id across two independently-durable stores. If loops.json is "
    "reset or restored while triage-log.jsonl survives, new loops reuse old ids and can "
    "inherit unrelated historical decisions — treat results as unreliable after any "
    "loops.json loss/restore (durable UUID/epoch identity is a tracked follow-up).",
    "A source with no surviving triage decisions is reported as N/A (no data), never as a "
    "measured 0% — absent evidence is not a clean result.",
    "Attribution is at review-`source` granularity, NOT per-lens: a review runs multiple "
    "lenses and no outcome record stores a lens tag, so a pointer names every lens that "
    "source uses.",
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
    """Loop ids that appear on more than one loop row. loop_store documents that
    concurrent Windows writers can mint colliding ids; a collision would let one
    triage decision be counted against several loops, so callers surface it."""
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
    latest: dict[object, dict] = {}
    for rec in triage:
        lid = rec.get("loop_id")
        if lid is None:
            continue
        latest[lid] = rec  # last write wins (chronological append order)

    quarantined = find_duplicate_loop_ids(loops)
    stats: dict[str, SourceStats] = {}
    for loop in loops:
        source = loop.get("source")
        if not source:
            continue
        lid = loop.get("id")
        if lid in quarantined:
            continue  # corrupted identity — attribute to nothing (never guess a source)
        s = stats.setdefault(source, SourceStats(source=source))
        s.total += 1
        rec = latest.get(lid)
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


def _pct(ratio: float | None) -> str:
    return "N/A" if ratio is None else f"{round(ratio * 100)}%"


def render_report(
    suggestions: list[Suggestion],
    stats: dict[str, SourceStats],
    *,
    duplicate_ids: set | None = None,
    as_json: bool = False,
) -> str:
    dups = sorted(duplicate_ids) if duplicate_ids else []
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
    print(
        render_report(
            suggestions, stats, duplicate_ids=duplicate_ids, as_json=args.json
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
