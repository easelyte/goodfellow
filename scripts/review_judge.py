"""Judge / ground-or-drop reconciliation for the two-stage adversarial review.

The judge is a SECOND `codex exec` call whose output is a per-finding *decision
table* — it never rewrites findings. This module owns the deterministic,
wrapper-side pieces (the bash bridge owns the two codex calls + flag gating):

  check_generator_contract() -> ok|passthrough
  build_judge_prompt()
  reconcile()  -> (reconciled_text, audit_rows)
  render_audit_section()
  publish_baseline() / publish_reconciled()

Finding identity = the generator's fenced ```json block carrying `finding_id`.
"keep" retains the block verbatim; a Tier-2/3 drop removes it; an auto-zero drop
removes it (reclassified, wins over retention); a Tier-1 non-auto-zero drop is
RETAINED and annotated so a real blocker can never be lost to a judge miss.

Failure is fail-OPEN: any judge/validation problem → the caller keeps the
generator baseline (already published) + a degradation banner.

Review artifacts are written to a local /tmp file shown only to the operator on
their own machine — there is no publish/egress surface, so the redactor is an
identity pass-through (see make_default_redactor).
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# This module lives at <plugin_root>/scripts/review_judge.py; its own directory
# is sys.path[0] when invoked as a script, so the sibling vendored modules import
# by bare name.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from atomic_write import write_text_atomic  # noqa: E402
from judge_decision_validator import (  # noqa: E402
    JudgeContractError,
    validate_decisions,
)

JUDGE_THRESHOLD = 4  # fixed shipping confidence threshold
DEGRADED_BANNER = (
    "> ⚠ JUDGE PASS DEGRADED — findings below are UNJUDGED; verify "
    "manually. Reason: {reason}."
)

# A fenced ```json ... ``` block. DOTALL so it spans lines; non-greedy body.
_JSON_BLOCK_RE = re.compile(r"```json\s*\n(?P<body>.*?)\n```", re.DOTALL)
# A `### ` FINDING sub-heading — `### B1.` / `### M2.` / `### N.` (severity letter
# B/M/N + optional number + a `.`/`)`/`:` delimiter). The generator emits each
# finding as a unit: this heading → prose → `## Tags:` line → ```json block. A
# dropped finding must strip the WHOLE unit — heading + prose + Tags + block — not
# just the json block, or the human-readable prose survives in OUT and
# misrepresents a judged-dropped finding as live.
#
# The pattern is deliberately NARROW: matching any `^###` would let an internal
# subsection inside a finding body (e.g. `### Reproduction`) be mistaken for the
# unit boundary, so a drop would strip only that suffix and leave the finding's
# real title + allegation visible. A heading that does not match the finding
# convention → no match → block-only strip (the safe fallback), never an
# over-strip. `## Tags:` (two hashes) is likewise never matched.
_SUBHEAD_LINE_RE = re.compile(r"(?m)^###\s+[BMN]\d*[.):]")
_VERDICT_RE = re.compile(
    r"^##\s*Verdict\s*\n+(?P<v>.+?)\s*$", re.IGNORECASE | re.MULTILINE
)


@dataclass
class GeneratorFinding:
    finding_id: str
    block: Dict[str, Any]
    raw: str  # the full ```json ... ``` fenced text
    start: int
    end: int

    @property
    def severity(self) -> str:
        return str(self.block.get("severity", "")).lower()

    @property
    def is_blocker(self) -> bool:
        return self.severity == "blocker"


@dataclass
class ReconcileResult:
    reconciled_text: str
    audit_rows: List[Dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Generator contract
# --------------------------------------------------------------------------- #
def extract_findings(text: str) -> List[GeneratorFinding]:
    findings: List[GeneratorFinding] = []
    for m in _JSON_BLOCK_RE.finditer(text):
        body = m.group("body")
        try:
            block = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            # An unparseable json block is a contract violation surfaced by
            # check_generator_contract (recorded as a None-id sentinel).
            findings.append(
                GeneratorFinding(
                    "", {"__unparseable__": True}, m.group(0), m.start(), m.end()
                )
            )
            continue
        fid = block.get("finding_id")
        findings.append(
            GeneratorFinding(
                finding_id=fid if isinstance(fid, str) else "",
                block=block,
                raw=m.group(0),
                start=m.start(),
                end=m.end(),
            )
        )
    return findings


def _verdict_is_clean(text: str) -> bool:
    m = _VERDICT_RE.search(text)
    if not m:
        return False
    v = m.group("v").strip().lower()
    return v.startswith("lgtm") and "with minor" not in v or v == "lgtm"


def check_generator_contract(text: str) -> Tuple[str, List[GeneratorFinding], str]:
    """Return (status, findings, reason).

    status == "ok"          -> findings all carry a unique parseable finding_id
    status == "passthrough" -> reason is 'generator-block-missing'; caller keeps
                               the baseline unjudged.
    """
    findings = extract_findings(text)

    # Prose-only: findings implied by a non-clean verdict but zero json blocks.
    if not findings:
        if _verdict_is_clean(text):
            return ("ok", [], "")
        return ("passthrough", [], "generator-block-missing")

    ids: List[str] = []
    for f in findings:
        if f.block.get("__unparseable__") or not f.finding_id:
            return ("passthrough", findings, "generator-block-missing")
        ids.append(f.finding_id)

    if len(set(ids)) != len(ids):
        # duplicate finding_id -> ambiguous -> passthrough
        return ("passthrough", findings, "generator-block-missing")

    return ("ok", findings, "")


# --------------------------------------------------------------------------- #
# Judge prompt
# --------------------------------------------------------------------------- #
def build_judge_prompt(
    findings: List[GeneratorFinding],
    context_text: str,
    hunks_text: Optional[str],
) -> str:
    """Build the judge prompt. `hunks_text` None/empty => --file/--files mode:
    the out-of-diff-boundary criterion is OMITTED."""
    has_hunks = bool(hunks_text and hunks_text.strip())

    drop_reasons = [
        "- `no-evidence`: the finding cannot cite supporting evidence in the "
        "provided code.",
        f"- `below-threshold`: your confidence `judge_score` is < {JUDGE_THRESHOLD}.",
        "- `auto-zero-category`: the finding ONLY adds docstrings / type-hints / "
        "comments, adds or removes imports, or proposes a more-specific exception "
        'type (these are Tier-3 by definition; set `reclassified_to`: "tier-3").',
    ]
    if has_hunks:
        drop_reasons.append(
            "- `out-of-diff-boundary`: the finding's SOLE cited evidence line is "
            "outside the changed hunks below. Drop it as out-of-diff-boundary "
            "UNLESS *you* independently determine the finding is causally "
            "load-bearing (the change enables, depends on, or interacts with the "
            "defect), in which case STILL record `drop_reason: out-of-diff-boundary` "
            "and set `causal_exception_valid: true`."
        )

    # The judge's independent causal-exception boolean. Only meaningful in
    # hunks/--diff mode (where out-of-diff-boundary exists); omitted in
    # --file/--files mode, consistent with the has_hunks gating.
    schema_fields = [
        '  "finding_id": "<the finding\'s id>",',
        '  "decision": "keep | drop",',
        '  "judge_score": <integer 0-10, your confidence the finding is a real, '
        "grounded defect>,",
        '  "drop_reason": "<one of the reasons below, or null when decision=keep>",',
    ]
    if has_hunks:
        schema_fields.append(
            '  "reclassified_to": "<\\"tier-3\\" only when '
            'drop_reason=auto-zero-category, else null>",'
        )
        schema_fields.append(
            '  "causal_exception_valid": <EXACT boolean true/false — true ONLY '
            "when decision=drop, drop_reason=out-of-diff-boundary AND you "
            "independently confirm the finding is causally load-bearing; "
            "false/null otherwise. Emit a real JSON boolean, never a string.>"
        )
        schema_intro = "Decision object schema (these six fields ONLY):"
    else:
        schema_fields.append(
            '  "reclassified_to": "<\\"tier-3\\" only when '
            'drop_reason=auto-zero-category, else null>"'
        )
        schema_intro = "Decision object schema (these five fields ONLY):"

    findings_json = json.dumps(
        [
            {
                "finding_id": f.finding_id,
                **{k: v for k, v in f.block.items() if k != "finding_id"},
            }
            for f in findings
        ],
        indent=2,
    )

    parts = [
        "You are the JUDGE pass of a two-stage adversarial code review. A "
        "generator already produced the findings below. Your job is to GROUND OR "
        "DROP each one — you must NOT invent, merge, rewrite, or omit findings.",
        "",
        "For EACH finding below, emit exactly one decision object. Output ONLY a "
        "single fenced ```json block containing a JSON array of decision objects, "
        "one per finding_id, nothing else.",
        "",
        schema_intro,
        "```json",
        "{",
        *schema_fields,
        "}",
        "```",
        "",
        "For each finding, require a concrete failure scenario (inputs/state -> "
        "wrong outcome) and the exact code evidence it rests on before you keep "
        "it. Drop reasons:",
        *drop_reasons,
        "",
        "PARTIAL-CONTEXT GUARDRAIL: the diff and inlined files are partial "
        "context. Do NOT drop-as-hallucination an entity merely because its "
        "definition is not shown, and do NOT keep a finding that only flags such "
        "an entity — judge on the evidence actually present.",
        "",
        "## Generator findings to judge",
        "```json",
        findings_json,
        "```",
        "",
        "## Code context",
        context_text,
    ]
    if has_hunks:
        parts += ["", "## Changed hunks (the in-scope diff boundary)", hunks_text]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #
def reconcile(
    generator_text: str,
    findings: List[GeneratorFinding],
    decisions: List[Dict[str, Any]],
) -> ReconcileResult:
    """Apply the validated decision table to the generator text.

    Precondition: decisions already validated by validate_decisions().
    """
    by_id = {d["finding_id"]: d for d in decisions}
    audit_rows: List[Dict[str, Any]] = []

    # Build replacements per finding block. We rewrite the text by walking the
    # findings in source order. Each finding owns its `### ` sub-heading + prose
    # + `## Tags:` line + json block; a DROP removes that whole unit, a
    # KEEP/RETAIN preserves it. Structural text between findings (the `##`
    # severity section headers, blank lines) is always retained verbatim.
    pieces: List[str] = []
    cursor = 0
    for f in findings:
        prose_start = _finding_prose_start(generator_text, cursor, f.start)
        # Retain inter-finding structural content up to this finding's heading.
        pieces.append(generator_text[cursor:prose_start])
        finding_prose = generator_text[prose_start : f.start]
        cursor = f.end
        dec = by_id[f.finding_id]
        decision = dec["decision"]
        score = dec["judge_score"]
        drop_reason = dec.get("drop_reason")
        reclassified_to = dec.get("reclassified_to")

        # The load-bearing verbatim escape requires BOTH the generator's
        # precondition flag AND the judge's independent confirmation
        # (`causal_exception_valid`). The generator flag alone does NOT defeat the
        # judge's authoritative out-of-diff-boundary scope gate — this strictly
        # narrows retention, never opens a new retention path.
        generator_load_bearing = f.block.get("out_of_scope_load_bearing") is True
        judge_causal_valid = dec.get("causal_exception_valid") is True

        if decision == "keep":
            pieces.append(finding_prose + f.raw)
            audit_rows.append(_row(f, "keep", score, None, None))
            continue

        # decision == drop
        if (
            drop_reason == "out-of-diff-boundary"
            and generator_load_bearing
            and judge_causal_valid
        ):
            # scope-fence load-bearing escape -> retain verbatim
            pieces.append(finding_prose + f.raw)
            audit_rows.append(
                _row(f, "keep", score, "out-of-diff-boundary(load-bearing)", None)
            )
            continue

        if drop_reason == "auto-zero-category":
            # reclassification WINS over blocker retention
            audit_rows.append(
                _row(
                    f, "drop", score, "auto-zero-category", reclassified_to or "tier-3"
                )
            )
            # Whole unit (prose + block) removed; leave a visible tombstone so a
            # reader sees the drop inline, not just in the trailing audit table.
            pieces.append(_tombstone(f, "auto-zero-category", score))
            continue

        if f.is_blocker:
            # Tier-1 evidence/score/boundary drop -> retain + annotate.
            annotated = (
                f.raw + f"\n\n> [judge-contested blocker: {drop_reason}, score {score}]"
            )
            pieces.append(finding_prose + annotated)
            audit_rows.append(_row(f, "retain-annotated", score, drop_reason, None))
            continue

        # Tier-2/Tier-3 drop -> remove the whole unit, leave a tombstone.
        audit_rows.append(_row(f, "drop", score, drop_reason, None))
        pieces.append(_tombstone(f, drop_reason, score))

    pieces.append(generator_text[cursor:])
    reconciled = "".join(pieces)
    reconciled = reconciled.rstrip() + "\n\n" + render_audit_section(audit_rows)
    return ReconcileResult(reconciled_text=reconciled, audit_rows=audit_rows)


def _finding_prose_start(text: str, lower: int, block_start: int) -> int:
    """Absolute index of the `### ` heading that opens this finding's unit.

    Prose-stripping is only SAFE for the adjacent layout (`### Bn.` heading →
    prose → `## Tags:` → block, one heading per block). To reject the detached
    layout (all headings first, then all blocks) and any ambiguous shape, this
    requires EXACTLY ONE finding heading in the gap `text[lower:block_start]`
    (``lower`` = the prior finding's block end). Zero headings → no prose to
    strip; two-or-more → the block cannot be unambiguously paired with a single
    heading (a detached layout puts N headings before the first block), so a
    strip would delete a DIFFERENT finding's prose. Either way, fall back to
    ``block_start`` (block-only strip — the safe behavior), never an over-strip.
    """
    region = text[lower:block_start]
    matches = list(_SUBHEAD_LINE_RE.finditer(region))
    if len(matches) != 1:
        return block_start
    return lower + matches[0].start()


def _decision_removes(f: GeneratorFinding, dec: Dict[str, Any]) -> bool:
    """True if this decision REMOVES the finding (leaves a tombstone) — as opposed
    to keep / retain-annotated blocker / load-bearing escape, which keep the block.
    """
    if dec.get("decision") != "drop":
        return False
    drop_reason = dec.get("drop_reason")
    # Mirror reconcile()'s BOTH-signal conjunction, or this function and reconcile
    # disagree on what counts as removed — drop_leaves_orphan_prose would then
    # misjudge a falsely-flagged out-of-diff finding as retained and leave
    # resurrectable prose (the failure documented below).
    if (
        drop_reason == "out-of-diff-boundary"
        and f.block.get("out_of_scope_load_bearing") is True
        and dec.get("causal_exception_valid") is True
    ):
        return False  # retained verbatim
    if drop_reason != "auto-zero-category" and f.is_blocker:
        return False  # retain-annotated (block kept)
    return True


def drop_leaves_orphan_prose(
    generator_text: str,
    findings: List[GeneratorFinding],
    decisions: List[Dict[str, Any]],
) -> bool:
    """True if a REMOVED finding sits in a layout whose `### ` prose headings do
    not pair 1:1 with the json blocks, so a dropped finding's heading + allegation
    would survive.

    Surviving dropped-finding prose is not inert: a downstream finding parser can
    treat an unpaired `### ` heading as a fallback finding (defaulting a Major to
    ship-blocking), so a judge-DROPPED finding would resurrect as a routed blocker
    — the audit table says "dropped" while the pipeline halts on it. The caller
    routes such an ambiguous layout to degraded passthrough (keep the unjudged
    baseline + banner) rather than emit a self-contradictory review.

    Three layouts, only the middle one is a risk:
      * NO `### Bn.` headings (pure-json findings) — total==0: removing the block
        removes the whole finding, nothing to orphan → NOT a risk.
      * ADJACENT — every finding's gap holds exactly ONE heading: reconcile strips
        each heading+prose+block unit cleanly → NOT a risk.
      * AMBIGUOUS — headings present but not 1:1 (a detached layout puts N headings
        before the first block): a heading will be left unpaired → RISK if any
        finding is removed.
    """
    by_id = {d["finding_id"]: d for d in decisions}
    cursor = 0
    gap_counts: List[int] = []
    any_removed = False
    for f in findings:
        gap_counts.append(
            len(_SUBHEAD_LINE_RE.findall(generator_text[cursor : f.start]))
        )
        any_removed = any_removed or _decision_removes(f, by_id.get(f.finding_id, {}))
        cursor = f.end
    if not any_removed or sum(gap_counts) == 0:
        return False
    return not all(n == 1 for n in gap_counts)


# A tombstone must carry NO generator-controlled free text: `short_label` /
# `normalized_text` are influenced by the (untrusted) reviewed diff, and the
# generator contract only requires `finding_id` to be a non-empty string — not a
# safe one. Interpolating either verbatim would let a crafted label inject a
# newline + fenced ```json block into the authoritative OUT, which a downstream
# finding parser would then read as a real finding (hostile reviewed-content
# boundary). So the id is hard-sanitized to a short safe charset and everything
# else in the line is either a controlled enum (`drop_reason`, validated) or an
# int (`score`).
_SAFE_FID_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _safe_fid(fid: str) -> str:
    return _SAFE_FID_RE.sub("", str(fid or ""))[:24] or "?"


def _tombstone(f: GeneratorFinding, drop_reason: Optional[str], score: int) -> str:
    """A one-line marker left where a dropped finding's unit was removed.

    Carries only a sanitized finding_id + the validated drop_reason enum + the
    integer score — never generator-controlled free text (hostile-content
    boundary).
    """
    return f"> [judge-dropped {_safe_fid(f.finding_id)}: {drop_reason}, score {int(score)}]\n"


def _row(
    f: GeneratorFinding,
    decision: str,
    score: int,
    drop_reason: Optional[str],
    reclassified_to: Optional[str],
) -> Dict[str, Any]:
    return {
        "finding_id": f.finding_id,
        "short_label": f.block.get("short_label", ""),
        "area": f.block.get("area", ""),
        "generator_severity": f.severity,
        "judge_score": score,
        "decision": decision,
        "drop_reason": drop_reason,
        "reclassified_to": reclassified_to,
    }


def _inert(value: Any) -> str:
    """Collapse a value to inert, single-line, table-safe Markdown text.

    The audit table interpolates generator-controlled cells (`finding_id`,
    `area`, generator `severity`) into the authoritative OUT. Left raw, a value
    carrying a newline + fenced ```json block would be re-parsed downstream as a
    forged finding — the same hostile-content boundary the tombstone already
    guards, reached via the audit table instead. Strip newlines/backticks/pipes
    so a cell can neither break the row nor open a code fence, and cap the length.
    """
    text = str("" if value is None else value)
    text = text.replace("`", "").replace("|", "\\|")
    text = " ".join(text.split())  # collapse ALL whitespace (incl. newlines)
    return text[:200]


def render_audit_section(audit_rows: List[Dict[str, Any]]) -> str:
    lines = ["## Judge audit", ""]
    if not audit_rows:
        lines.append("_No findings to judge._")
        return "\n".join(lines) + "\n"
    lines.append(
        "| finding_id | decision | severity | score | drop_reason | reclassified_to | area |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    # decision / drop_reason / reclassified_to are controlled enums and score is
    # an int, but finding_id / severity / area are generator-controlled — inert
    # every cell so no path can inject Markdown into the authoritative table.
    for r in audit_rows:
        cells = {k: _inert(v) for k, v in r.items()}
        lines.append(
            "| {finding_id} | {decision} | {generator_severity} | {judge_score} | "
            "{drop_reason} | {reclassified_to} | {area} |".format(**cells)
        )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Publish lifecycle (redactor is identity — local-only artifacts, no egress)
# --------------------------------------------------------------------------- #
class RedactionUnavailable(RuntimeError):
    """Redactor could not run — the caller fails CLOSED (wrapper exit 14).

    Unreachable with the identity redactor (which never raises); retained so the
    publish machinery stays byte-close to its source and a future project-authored
    redactor can restore fail-closed behavior without restructuring.
    """


def _redact(text: str, redactor: Optional[Callable[[str], str]]) -> str:
    if redactor is None:
        raise RedactionUnavailable("no redactor supplied")
    try:
        return redactor(text)
    except RedactionUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - any redactor failure is fail-closed
        raise RedactionUnavailable(str(exc)) from exc


def publish_baseline(
    generator_text: str,
    out_path: Path,
    redactor: Optional[Callable[[str], str]],
) -> None:
    """Redact the generator output and atomically publish it to OUT as the
    fail-open baseline. Raises RedactionUnavailable on redaction failure so the
    wrapper withholds OUT + exits 14."""
    redacted = _redact(generator_text, redactor)
    write_text_atomic(out_path, redacted)


def publish_reconciled(
    reconciled_text: str,
    out_path: Path,
    redactor: Optional[Callable[[str], str]],
    sidecar_path: Optional[Path] = None,
    audit_rows: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Write the best-effort .jsonl sidecar (derived), then atomically replace
    OUT with the reconciled text. The in-OUT `## Judge audit` section is
    authoritative; the sidecar is a convenience."""
    if sidecar_path is not None and audit_rows is not None:
        try:
            sidecar_path.write_text(
                "\n".join(json.dumps(r) for r in audit_rows) + "\n", encoding="utf-8"
            )
        except OSError:
            pass  # sidecar is best-effort; never blocks the authoritative OUT
    redacted = _redact(reconciled_text, redactor)
    write_text_atomic(out_path, redacted)


def make_default_redactor() -> Callable[[str], str]:
    """Return an identity (pass-through) redactor.

    Review artifacts are local /tmp files shown only to the operator on their own
    machine — there is no publish or egress surface, so no redaction is required.
    Any secret in the output would be the user's OWN credential in the user's OWN
    repo, written to the user's OWN /tmp (identical to a plain `codex exec review`
    run). A project that later wants review artifacts scrubbed of the reviewed
    repo's secrets can supply a real redactor callable here over user-configured
    patterns; that is a separate feature, not part of this pipeline.
    """
    return lambda text: text


# --------------------------------------------------------------------------- #
# CLI dispatch (invoked by codex-bridge.sh)
# --------------------------------------------------------------------------- #
# Exit codes (mapped by the wrapper):
#   0  ok
#   3  usage / internal error
#   14 redaction fail-closed (unreachable with the identity redactor)
#   20 judge passthrough requested (wrapper: keep baseline + banner, exit 0)
def _cli(argv: List[str]) -> int:
    if not argv:
        print(
            "usage: review_judge.py {contract|judge-prompt|reconcile|baseline|banner} ...",
            file=sys.stderr,
        )
        return 3
    cmd, rest = argv[0], argv[1:]

    if cmd == "contract":
        # contract <gen_out>  -> stdout "ok" | "passthrough:<reason>"
        gen_text = Path(rest[0]).read_text(encoding="utf-8")
        status, _findings, reason = check_generator_contract(gen_text)
        print("ok" if status == "ok" else f"passthrough:{reason}")
        return 0 if status == "ok" else 20

    if cmd == "banner":
        # banner <out> <reason>  -> prepend the degradation banner to OUT.
        out = Path(rest[0])
        reason = rest[1] if len(rest) > 1 else "unknown"
        body = out.read_text(encoding="utf-8")
        write_text_atomic(out, DEGRADED_BANNER.format(reason=reason) + "\n\n" + body)
        return 0

    if cmd == "baseline":
        # baseline <gen_out> <final_out>  -> publish baseline (fail-closed on
        # redaction; unreachable with the identity redactor)
        gen_text = Path(rest[0]).read_text(encoding="utf-8")
        try:
            publish_baseline(gen_text, Path(rest[1]), make_default_redactor())
        except RedactionUnavailable as exc:
            print(f"class:review-output-redaction-failed {exc}", file=sys.stderr)
            return 14
        return 0

    if cmd == "judge-prompt":
        # judge-prompt <gen_out> <context_file> <hunks_file|NONE> <out_prompt>
        gen_text = Path(rest[0]).read_text(encoding="utf-8")
        status, findings, reason = check_generator_contract(gen_text)
        if status != "ok":
            print(f"passthrough:{reason}", file=sys.stderr)
            return 20
        context = Path(rest[1]).read_text(encoding="utf-8")
        hunks = None if rest[2] == "NONE" else Path(rest[2]).read_text(encoding="utf-8")
        prompt = build_judge_prompt(findings, context, hunks)
        Path(rest[3]).write_text(prompt, encoding="utf-8")
        return 0

    if cmd == "reconcile":
        # reconcile <gen_out> <judge_out> <final_out> <sidecar>
        gen_text = Path(rest[0]).read_text(encoding="utf-8")
        judge_text = Path(rest[1]).read_text(encoding="utf-8")
        final_out = Path(rest[2])
        sidecar = Path(rest[3]) if len(rest) > 3 and rest[3] != "NONE" else None

        status, findings, reason = check_generator_contract(gen_text)
        if status != "ok":
            print(f"passthrough:{reason}", file=sys.stderr)
            return 20

        # Extract the judge's decision array from its (possibly fenced) output.
        m = _JSON_BLOCK_RE.search(judge_text)
        raw = m.group("body") if m else judge_text
        try:
            decisions = json.loads(raw)
            decisions = validate_decisions(decisions, [f.finding_id for f in findings])
        except (json.JSONDecodeError, ValueError, JudgeContractError) as exc:
            print(f"judge-contract-fail:{exc}", file=sys.stderr)
            return 20  # wrapper falls back to baseline + banner

        # Ambiguous layout guard: if a DROPPED finding's prose can't be cleanly
        # stripped, reconciling would leave an allegation that a downstream parser
        # re-routes as a ship-blocking finding — contradicting the judge. Fail
        # OPEN to the unjudged baseline + banner instead.
        if drop_leaves_orphan_prose(gen_text, findings, decisions):
            print("passthrough:ambiguous-layout-unstrippable-drop", file=sys.stderr)
            return 20

        result = reconcile(gen_text, findings, decisions)
        try:
            publish_reconciled(
                result.reconciled_text,
                final_out,
                make_default_redactor(),
                sidecar_path=sidecar,
                audit_rows=result.audit_rows,
            )
        except RedactionUnavailable as exc:
            # baseline already on disk -> degrade to unjudged, not exit 14
            print(f"reconcile-redaction-fail:{exc}", file=sys.stderr)
            return 20
        return 0

    print(f"unknown subcommand: {cmd}", file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
