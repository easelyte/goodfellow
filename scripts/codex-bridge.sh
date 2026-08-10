#!/usr/bin/env bash
# Goodfellow Codex bridge — two-stage generator+judge adversarial review, with a
# single-Claude fallback when Codex is unavailable.
#
# Usage: codex-bridge.sh --kind <spec|plan|diff> [--model <sonnet|opus|haiku>]
#        [--include-aesthetic] [--commit <sha>] [--base <branch>] [--uncommitted]
#        [--file <path>] [-- <prompt>]
#
# The Codex path inlines the diff/commit/file body into a generator prompt (an
# adversarial reviewer that emits per-finding structured ```json blocks), runs a
# second Codex "judge" pass that grounds-or-drops each finding, and reconciles
# the two into the final review. Every judge/validation problem fails OPEN to the
# unjudged generator baseline + a degradation banner — the judge can never turn a
# real finding into a silent drop or a Tier-2 into a ship-blocking halt.
#
# Output contract:
#   success -> the LAST stdout line is a readable review-artifact path.
#   failure -> a nonzero exit AND the last stdout line is `REVIEW_FAILED <rc>
#              <class>` (never a path). Callers MUST check `$?` / reject the
#              `REVIEW_FAILED ` prefix before reading the artifact path.
#
# MODEL ($GOODFELLOW_REVIEW_MODEL, default sonnet) is a CLAUDE model id, only ever
# passed to the Claude fallback reviewer. The Codex path must NOT receive a Claude
# model name (codex expects a GPT model id); it stays on codex's configured
# default unless $GOODFELLOW_CODEX_MODEL is set to a GPT id.
#
# Review artifacts are LOCAL /tmp files shown only to the operator on their own
# machine — there is no publish/egress surface, so the review pipeline uses an
# identity (pass-through) redactor (see review_judge.make_default_redactor).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RJ="${SCRIPT_DIR}/review_judge.py"

KIND=""
MODEL="${GOODFELLOW_REVIEW_MODEL:-sonnet}"
# GPT model id for the Codex path only. Empty => codex uses its configured default.
CODEX_MODEL="${GOODFELLOW_CODEX_MODEL:-}"
INCLUDE_AESTHETIC=""
COMMIT=""
BASE=""
UNCOMMITTED=""
FILE=""
PROMPT=""
MIN_VERSION="0.120.0"
# Per-stage codex timeout (seconds). The pipeline runs codex TWICE (generator +
# judge), sequentially, so total wall-clock is up to 2x this value. This is a
# PER-PASS bound, never an end-to-end budget.
STAGE_TIMEOUT="${GOODFELLOW_CODEX_STAGE_TIMEOUT:-300}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --kind) KIND="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --include-aesthetic) INCLUDE_AESTHETIC="1"; shift ;;
    --commit) COMMIT="$2"; shift 2 ;;
    --base) BASE="$2"; shift 2 ;;
    --uncommitted) UNCOMMITTED="1"; shift ;;
    --file) FILE="$2"; shift 2 ;;
    --) shift; PROMPT="$*"; break ;;
    *) echo "Unknown arg: $1" >&2; FAIL_CLASS="bad-args"; exit 1 ;;
  esac
done

OUTFILE=$(mktemp /tmp/goodfellow-review-XXXXXX)
GEN_TMP=$(mktemp /tmp/goodfellow-gen-XXXXXX)
CTX_TMP=$(mktemp /tmp/goodfellow-ctx-XXXXXX)
HUNKS_TMP=$(mktemp /tmp/goodfellow-hunks-XXXXXX)
GEN_PROMPT=$(mktemp /tmp/goodfellow-genprompt-XXXXXX)
LOG=$(mktemp /tmp/goodfellow-log-XXXXXX)

# --- REVIEW_FAILED sentinel contract ----------------------------------------
# On ANY nonzero exit, emit exactly one `REVIEW_FAILED <exit_code> <class>` line
# so a caller doing `OUT=$(bridge ...)` can never silently proceed with a false
# "reviewed" claim: the sentinel is not path-like and the exit code is nonzero.
# FAIL_CLASS is set immediately before each nonzero exit.
FAIL_CLASS="${FAIL_CLASS:-}"
_sentinel_emitted=0
on_exit() {
  local rc=$?
  rm -f "$GEN_TMP" "$CTX_TMP" "$HUNKS_TMP" "$GEN_PROMPT" "$LOG" "${JUDGE_PROMPT:-}" "${JUDGE_OUT:-}" 2>/dev/null || true
  if [[ "$rc" -ne 0 && "$_sentinel_emitted" != "1" ]]; then
    _sentinel_emitted=1
    printf 'REVIEW_FAILED %s %s\n' "$rc" "${FAIL_CLASS:-unknown-failure}"
  fi
}
trap on_exit EXIT

if [[ -n "$FILE" && ! -f "$FILE" ]]; then
  echo "ERROR: --file target not found: $FILE" >&2
  FAIL_CLASS="bad-args"; exit 1
fi

has_codex() {
  [[ "${GOODFELLOW_CODEX:-1}" != "0" ]] && command -v codex &>/dev/null
}

check_version() {
  local ver
  ver=$(codex --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
  if [[ -z "$ver" ]]; then
    echo "WARNING: Could not detect Codex version" >&2
    return
  fi
  local lowest
  lowest=$(printf '%s\n%s\n' "$MIN_VERSION" "$ver" | sort -V | head -1)
  if [[ "$lowest" == "$ver" && "$ver" != "$MIN_VERSION" ]]; then
    echo "WARNING: Codex $ver < minimum $MIN_VERSION — output may be degraded" >&2
  fi
}

# Collect the diff/content the review should cover, into CTX_TMP. This is the
# ground-truth context the reviewer is told to treat as authoritative (it must
# NOT re-run git from inside the sandbox to verify). The raw diff (hunks) is ALSO
# captured to HUNKS_TMP — the judge's out-of-diff-boundary rule needs the diff
# boundary WITHOUT the enrichment (D2 full files / D1 digest) that CTX_TMP gains.
build_context() {
  : > "$HUNKS_TMP"
  {
    if [[ -n "$FILE" ]]; then
      echo "**Mode:** single file review — \`$FILE\`"
      echo ""
      echo '```'
      cat "$FILE" 2>/dev/null || echo "(could not read file $FILE)"
      echo '```'
    elif [[ -n "$COMMIT" ]]; then
      echo "**Mode:** single commit \`$COMMIT\`"
      echo ""
      git show --no-ext-diff "$COMMIT" > "$HUNKS_TMP" 2>/dev/null || echo "(could not read commit $COMMIT)" > "$HUNKS_TMP"
      echo '```diff'
      cat "$HUNKS_TMP"
      echo '```'
    elif [[ -n "$BASE" ]]; then
      echo "**Mode:** branch diff against \`$BASE\`"
      echo ""
      git diff --no-ext-diff "$BASE"...HEAD > "$HUNKS_TMP" 2>/dev/null || echo "(could not diff against $BASE)" > "$HUNKS_TMP"
      echo '```diff'
      cat "$HUNKS_TMP"
      echo '```'
    elif [[ -n "$UNCOMMITTED" ]]; then
      echo "**Mode:** uncommitted changes"
      echo ""
      { git diff HEAD 2>/dev/null; git diff --cached 2>/dev/null; } > "$HUNKS_TMP" || true
      echo '```diff'
      cat "$HUNKS_TMP"
      echo '```'
    fi
  } > "$CTX_TMP"

  enrich_context
}

# Append deterministic-tool (D1) + full-file/cross-ref (D2) context for committed
# revisions (--commit / --base). Working-tree (--uncommitted) and single-file
# (--file) modes are skipped: D1/D2 resolve committed blobs at a ref. Best-effort
# — any resolver failure degrades to the plain diff context, never a hard error.
enrich_context() {
  local mode="" rev="" diff_range="" name_status=""
  if [[ -n "$COMMIT" ]]; then
    mode="commit"; rev="$COMMIT"; diff_range="$COMMIT"
    name_status="$(git show --no-ext-diff --name-status --format= "$COMMIT" 2>/dev/null || true)"
  elif [[ -n "$BASE" ]]; then
    mode="diff"; rev="HEAD"; diff_range="${BASE}...HEAD"
    name_status="$(git diff --no-ext-diff --name-status "${BASE}...HEAD" 2>/dev/null || true)"
  else
    return 0
  fi
  [[ -z "$name_status" ]] && return 0

  local changed_args=()
  local line
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    changed_args+=(--changed "$line")
  done <<< "$name_status"
  [[ ${#changed_args[@]} -eq 0 ]] && return 0

  # D1 — deterministic static-analysis digest (analyzers degrade gracefully).
  # Executing analyzers stay OFF unless the operator opts into the trust model.
  local trust_flag=()
  [[ "${GOODFELLOW_TRUST_ANALYZERS:-}" == "1" ]] && trust_flag=(--trust-analyzers)
  local d1
  d1="$(python3 "${SCRIPT_DIR}/review_prepass.py" --mode "$mode" --workdir . \
        --rev "$diff_range" "${trust_flag[@]}" "${changed_args[@]}" 2>/dev/null || true)"
  if [[ -n "$d1" ]]; then
    { echo ""; echo "$d1"; } >> "$CTX_TMP"
  fi

  # D2 — full-file + literal-token cross-reference context.
  local d2
  d2="$(python3 "${SCRIPT_DIR}/review_context.py" --mode "$mode" --workdir . \
        --rev "$rev" --diff-range "$diff_range" "${changed_args[@]}" 2>/dev/null || true)"
  if [[ -n "$d2" ]]; then
    { echo ""; echo "$d2"; } >> "$CTX_TMP"
  fi
}

# True when the review has a diff boundary (diff/commit/base/uncommitted). --file
# mode has no diff boundary, so the judge's out-of-diff-boundary criterion is
# omitted (see review_judge.build_judge_prompt has_hunks gating).
has_hunks() {
  [[ -z "$FILE" ]] && [[ -n "$COMMIT" || -n "$BASE" || -n "$UNCOMMITTED" ]]
}

# Inject project principle CONTENT (goodfellow's own knowledge/principles*.md) so
# the reviewer can cite P-NNN violations. Best-effort: a resolver failure degrades
# to no principle section rather than failing the review.
emit_principles() {
  local plugin_root="${CLAUDE_PLUGIN_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
  python3 "${SCRIPT_DIR}/principles_context.py" --emit --plugin-root "$plugin_root" \
    --project-root . 2>/dev/null || true
}

build_generator_prompt() {
  local framing=""
  case "$KIND" in
    diff|code)
      framing="<stance>
You are an adversarial reviewer of code changes. Your job is to break confidence in the change, not to validate it. Default to skepticism — assume the change can fail in subtle, high-cost, or user-visible ways until the evidence says otherwise. Do not give credit for good intent, partial fixes, or likely follow-up work. Happy-path-only is a real weakness.
</stance>

<attack_surface>
Prioritize failures that are expensive, dangerous, or hard to detect:
- auth, permissions, tenant isolation, and trust boundaries
- data loss, corruption, duplication, and irreversible state changes
- rollback safety, retries, partial failure, and idempotency gaps
- race conditions, ordering assumptions, stale state, and re-entrancy
- empty-state, null, timeout, and degraded dependency behavior
- version skew, schema drift, migration hazards, compatibility regressions
- observability gaps that would hide failure or make recovery harder
</attack_surface>

<review_method>
Actively try to disprove the change. Trace how bad inputs, retries, concurrent actions, or partially completed operations move through the code. Look for violated invariants, missing guards, unhandled failure paths, and assumptions that stop being true under stress.
</review_method>

<finding_bar>
Each finding answers:
1. What can go wrong?
2. Why is this code path vulnerable?
3. What is the likely impact?
4. What concrete change reduces the risk?
Cite file:line for every finding.
</finding_bar>"
      ;;
    spec)
      framing="<stance>
You are an adversarial reviewer of a software specification. Your job is to break confidence in the spec, not to compliment the writing. Default to skepticism — assume any ambiguity will be misimplemented and any unstated assumption will be violated. Do not give credit for likely-future-clarifications.
</stance>

<attack_surface>
Prioritize spec defects that cause re-work or production failure:
- contradictions between sections; ambiguous success criteria
- undefined behavior in edge cases (empty input, partial failure, timeout)
- hidden coupling between components stated as independent
- scalability or performance assumptions stated as fact without evidence
- missing rollback, migration, or backwards-compatibility paths
- missing observability/audit requirements for failure recovery
- scope boundaries that overlap or leave gaps
</attack_surface>

<review_method>
Trace how the spec would be implemented under stress. For each requirement: does it survive partial-failure, scaling, edge inputs, concurrent actors? For each cross-section reference: do the sections agree or contradict? Do not propose new features; surface gaps in what is already specified.
</review_method>

<finding_bar>
Report only material findings — issues that would cause re-work, production incidents, or implementation paralysis. Cite section/quote for every finding.
</finding_bar>"
      ;;
    plan)
      framing="<stance>
You are an adversarial reviewer of an implementation plan. Your job is to break confidence in the plan before execution begins, not to compliment the planning. Default to skepticism — assume each task will be executed by a subagent with no extra context, and any gap, ambiguity, or wrong assumption will silently propagate into broken code.
</stance>

<attack_surface>
Prioritize plan defects that will cause execution failure:
- missing prerequisites; tasks that depend on unmet earlier-task outputs
- wrong execution order — task N references something only created by task N+M
- parallel-conflict risks — tasks marked parallel that mutate shared state
- missing tests; missing rollback paths; missing migration safety
- risky assumptions about library/framework/API behavior stated as fact
- missing observability — failures during execution would be invisible
- API/symbol/path drift — plan references things that don't exist or have different signatures than claimed
</attack_surface>

<review_method>
Trace task execution in order: at the start of each task, does the executor have everything it needs from prior tasks? Identify any task whose preconditions are not produced by an earlier task. For parallel tracks, identify any shared mutable state. Verify any factual claim about library/API behavior; if you are unsure, flag it explicitly.
</review_method>

<finding_bar>
Report only material findings — issues that would cause a task to fail, produce wrong output, or block subsequent tasks. Cite plan section for every finding.
</finding_bar>"
      ;;
    *)
      echo "unknown review kind: $KIND" >&2
      FAIL_CLASS="bad-args"; exit 2
      ;;
  esac

  # Tier-3 emission cap.
  local tier3_directive prioritization_cap
  if [[ "$INCLUDE_AESTHETIC" == "1" ]]; then
    tier3_directive="EMIT Tier 3 findings up to the prioritization cap below."
    prioritization_cap="Prioritization: cap your output to the top 3 Tier 1 (blocker) findings, top 5 Tier 2 (major) findings, and top 5 Tier 3 (minor) findings. Force prioritization within tiers. If you find more in a tier, drop the lowest-impact entries; do not pad to fill the cap."
  else
    tier3_directive="SUPPRESS Tier 3 findings entirely; do not emit any minor findings."
    prioritization_cap="Prioritization: cap your output to the top 3 Tier 1 (blocker) findings and top 5 Tier 2 (major) findings. Tier 3 (minor) findings are suppressed for this review; do not emit any. Force prioritization within tiers. If you find more in a tier, drop the lowest-impact entries; do not pad to fill the cap."
  fi

  # Verify-by-exploration mandate for code/diff reviews.
  local verify_mandate=""
  if [[ ( "$KIND" == "diff" || "$KIND" == "code" ) ]] && has_hunks; then
    verify_mandate="
<verification_mandate>
The diff below was captured by the host shell and embedded inline. Do NOT re-run \`git diff\` from inside the sandbox to verify — treat the inlined diff as authoritative; do not re-derive the diff itself.
- Explore the repository rather than reasoning from the inlined diff alone. Use \`rg\` to sweep callers, references, and variants of changed symbols, highest-impact first: exported/public symbols, changed function signatures, changed path/constant/field names, and anything feeding a filesystem, SQL, shell, or network sink. Trivial local-only renames need no sweep. Explore within the per-pass time budget; do not impose a numeric exploration budget.
- Coverage honesty is required. If the change is too large to sweep every priority symbol, name each changed symbol you did not fully explore instead of returning a silent LGTM.
- State your caller-set coverage EXPLICITLY for each changed symbol: either affirm you swept its full caller set (e.g. \"swept all N callers of <symbol>\"), or name the specific callers or symbols you could not fully explore. An unstated sweep is treated as incomplete, not complete.
- REQUIRED structured coverage report. For each changed exported symbol you swept, emit one fenced \`coverage\` block with machine-readable key=value fields:
  \`\`\`coverage
  symbol=<name> callers_total=<N> callers_examined=<M> unexplored=<comma-separated callers, or \"none\">
  \`\`\`
  Set \`callers_examined=callers_total\` and \`unexplored=none\` only when you actually swept the entire caller set.
- Read whole touched files and their immediate callers, not just the changed hunks. Trace untrusted input to its sink for every changed path that ingests external data. Check for test theater by confirming that a test in the diff exercises the real exported symbol.
- File any cross-file finding that meets the causal exception (the change enables, depends on, or interacts with the defect) with an explicit \"Out-of-scope but load-bearing\" callout and \`out_of_scope_load_bearing: true\` in its structured finding block.
</verification_mandate>"
  fi

  # Scope fence per mode.
  local scope_fence_body
  if [[ -n "$FILE" ]]; then
    scope_fence_body="This review covers the ENTIRE file body shown above. There is no diff boundary in --file mode; all content shown is in scope."
  elif [[ "$KIND" == "diff" || "$KIND" == "code" ]]; then
    scope_fence_body="This review covers the changes introduced by THIS PR — the changed HUNKS within the diff shown above are in scope. Unchanged code in a touched file is OUT OF SCOPE (the scope fence is hunk-level, not file-level). Pre-existing code the diff does not touch is OUT OF SCOPE."
  else
    scope_fence_body="This review covers the document above. \"Out of scope\" means content the document itself marks as non-goals, open questions, or deferred follow-ups. Findings about deferred content are at most Tier 2."
  fi

  # OOSLB gating: outside --diff code, a causally-reached out-of-scope finding is
  # Tier 1 only; never set the flag true for a Tier 2/3 finding there.
  local ooslb_rule causal_override=""
  ooslb_rule="Set it to \`true\` ONLY for a Tier 1 (load-bearing) finding that meets the causal exception (the reviewed change directly enables, depends on, or interacts with the defect). Outside a code diff, causally reached out-of-scope findings are Tier 1 only, so never set it \`true\` for a Tier 2 or Tier 3 finding."
  if [[ ( "$KIND" == "diff" || "$KIND" == "code" ) ]] && has_hunks; then
    ooslb_rule="Set it to \`true\` at any severity only when the finding meets the causal exception: the reviewed change directly enables, depends on, or interacts with the defect."
    causal_override="

Code-diff causal exception override: A defect that the reviewed change directly enables, depends on, or interacts with is IN SCOPE and may be filed at any severity. Unrelated out-of-scope defects remain excluded."
  fi

  local principles
  principles="$(emit_principles)"

  local skeptic_check="SKEPTICISM CHECK: If you return LGTM (or any verdict with no blockers and no major issues), you MUST end your response with three concrete things you verified — citing specific file:line references or quoted spec/plan sections. Approval without this evidence block will be treated as insufficient review."

  {
    echo "$framing"
    echo "$verify_mandate"
    echo ""
    echo "<severity_rubric>"
    echo "Bucket every finding into exactly one of three tiers before reporting. Map to the structured \`severity\` field as follows:"
    echo ""
    echo "TIER 1 — LOAD-BEARING (severity: blocker, ship_blocking: true)"
    echo "The finding describes a concrete failure path where the system produces wrong results, loses data, breaks a documented contract, exposes a security surface, or wedges. You must cite: the named input or trigger condition; the exact location (file:line for code, section/quote for a spec/plan); and the observable bad outcome. If you cannot cite all three, the finding is not Tier 1."
    echo ""
    echo "TIER 2 — DEFENSE-IN-DEPTH (severity: major, ship_blocking: false)"
    echo "The finding is a real correctness or robustness gap, but realizing it requires a narrow precondition: a specific race window, operator misuse outside the documented contract, a future schema change, a partial-failure scenario the current operational envelope does not produce. File as a follow-up; do not gate the ship."
    echo ""
    echo "TIER 3 — AESTHETIC / CONSISTENCY (severity: minor, ship_blocking: false)"
    echo "Wording, doc cross-reference drift, style-guide nits, naming choices, comment density, log-string phrasing."
    echo ""
    echo "${tier3_directive}"
    echo ""
    echo "Mapping rule: the tier determines the severity field. Do not emit \`severity: blocker\` for a Tier 2 finding to force operator attention — if the operator should see it before ship, it is by definition Tier 1 and must carry the full Tier 1 citation triad."
    echo "</severity_rubric>"
    echo ""
    echo "<scope_fence>"
    echo "${scope_fence_body}"
    echo ""
    echo "OUT-OF-SCOPE issue handling:"
    echo "  - If you spot a Tier 1 load-bearing issue (genuinely will break the system) in out-of-scope content, file it with an explicit \"Out-of-scope but load-bearing\" callout — load-bearing findings escape the scope fence."
    echo "  - If you spot a Tier 2 or Tier 3 issue in out-of-scope content, do NOT file it."
    echo "${causal_override}"
    echo "</scope_fence>"
    echo ""
    echo "<grounding_rules>"
    echo "Be aggressive, but stay grounded. Every finding must be defensible from the provided context. Do not invent files, lines, sections, code paths, incidents, behaviors, or attack chains you cannot support. If a conclusion depends on inference, state that explicitly and keep your confidence honest."
    echo "</grounding_rules>"
    echo ""
    echo "<calibration>"
    echo "Prefer one strong, well-grounded finding over several weak or speculative ones. Do not dilute serious issues with filler. If the change/spec/plan looks safe, say so directly and return no findings — do not manufacture critique to fill space."
    echo "</calibration>"
    echo ""
    echo "<partial_context_guardrail>"
    echo "The diff and inlined files are partial context. Do NOT flag an entity (function, type, config, import, symbol) merely because its definition/initialization is not shown — it may be defined elsewhere in the repo. Either Read to confirm, or do not flag."
    echo "</partial_context_guardrail>"
    echo ""
    echo "<auto_zero_categories>"
    echo "The following are Tier 3 by definition and must NOT be surfaced as blockers: suggestions that only add docstrings/type-hints/comments, only add or remove imports, or only propose a more-specific exception type."
    echo "</auto_zero_categories>"
    echo ""
    echo "<coverage_breadth>"
    echo "Spec/plan/test/skill/SKILL.md/config files included in a code diff are IN SCOPE for data-integrity and contract findings — do not treat them as out-of-scope just because the review is code-focused."
    echo "</coverage_breadth>"
    if [[ -n "$principles" ]]; then
      echo ""
      echo "<project_principles>"
      echo "Also flag violations of the project's own design principles below. Cite a violated principle by its \`P-NNN\` id with a one-line summary; do NOT inline the full rule text, and do NOT invent any other rule-id citation form."
      echo ""
      echo "$principles"
      echo "</project_principles>"
    fi
    echo ""
    echo "<final_check>"
    echo "Before finalizing, verify each finding is: adversarial rather than stylistic; tied to a concrete location (file:line, section, or quote); plausible under a real failure scenario; actionable for the author fixing it. Drop any finding that fails these checks."
    echo "</final_check>"
    echo ""
    echo "$skeptic_check"
    echo ""
    echo "Output format (markdown):"
    echo ""
    echo "$prioritization_cap"
    echo ""
    echo "## Verdict"
    echo "{LGTM | LGTM with minor notes | Changes requested | Major rework}"
    echo ""
    echo "## Blockers"
    echo "{list, or 'None'}"
    echo ""
    echo "## Major issues"
    echo "{list, or 'None'}"
    echo ""
    echo "## Minor"
    echo "{list, or 'None'}"
    echo ""
    echo "## Verified (only if no blockers/major)"
    echo "{three concrete items}"
    echo ""
    echo "## Per-finding schema (REQUIRED for every Blocker / Major / Minor)"
    echo ""
    echo "A second JUDGE pass grounds-or-drops each finding, so every finding MUST carry a unique \`finding_id\` (\`F1\`, \`F2\`, … in emission order). A finding without a parseable \`finding_id\` block cannot be judged and forces the whole review to a degraded, UNJUDGED pass. Emit each finding as a \`### <B|M|N><n>. <title>\` heading, then prose, then this structured block:"
    echo ""
    echo '```json'
    echo '{'
    echo '  "finding_id": "F1",'
    echo '  "severity": "blocker | major | minor",'
    echo '  "ship_blocking": true,'
    echo '  "out_of_scope_load_bearing": false,'
    echo '  "area": "<file path or component>",'
    echo '  "short_label": "<≤8 words>",'
    echo '  "normalized_text": "<finding body, ~50-300 words>"'
    echo '}'
    echo '```'
    echo ""
    echo "The \`ship_blocking\` value MUST follow the rubric: true for Tier 1 (blocker) findings, false for Tier 2 and Tier 3 (major and minor). The template shows \`true\` as a syntactic placeholder, not a default — emit \`false\` for any Tier 2/Tier 3 finding you report."
    echo ""
    echo "\`out_of_scope_load_bearing\` MUST be a JSON boolean and defaults to \`false\`. ${ooslb_rule}"
    echo ""
    echo "---"
    echo ""
    echo "## Context to review"
    echo ""
    cat "$CTX_TMP"
    if [[ -n "$PROMPT" ]]; then
      echo ""
      echo "## Additional reviewer notes from operator"
      echo ""
      echo "$PROMPT"
    fi
  } > "$GEN_PROMPT"
}

run_codex() {
  check_version
  build_context
  build_generator_prompt

  # Dry-run hook (prompt-assembly tests): emit the assembled generator prompt and
  # exit without calling codex.
  if [[ -n "${GOODFELLOW_CODEX_DRY_RUN:-}" ]]; then
    cat "$GEN_PROMPT" > "$OUTFILE"
    return 0
  fi

  local model_args=()
  # Codex/GPT model id ONLY — never $MODEL (a Claude id). Empty => codex default.
  [[ -n "$CODEX_MODEL" ]] && model_args+=(--model "$CODEX_MODEL")

  # --- Stage 1: generator (free-form codex exec, inline context) --------------
  local grc=0
  timeout "$STAGE_TIMEOUT" codex exec --sandbox read-only "${model_args[@]}" \
    -o "$GEN_TMP" - < "$GEN_PROMPT" >>"$LOG" 2>&1 || grc=$?
  if [[ $grc -ne 0 ]]; then
    echo "ERROR: Codex generator pass timed out or failed (exit $grc)" >&2
    tail -20 "$LOG" >&2 || true
    FAIL_CLASS="codex-exec-failed"; exit 3
  fi
  if [[ ! -s "$GEN_TMP" ]]; then
    echo "ERROR: Codex generator produced no output" >&2
    FAIL_CLASS="empty-output"; exit 4
  fi

  # --- Publish the fail-open baseline (identity redactor) ---------------------
  local brc=0
  python3 "$RJ" baseline "$GEN_TMP" "$OUTFILE" || brc=$?
  if [[ $brc -ne 0 ]]; then
    echo "ERROR: baseline publish failed (rc=$brc)" >&2
    FAIL_CLASS="baseline-publish-failed"; exit "$brc"
  fi

  # --- Stage 2: judge (only when the generator contract holds) ----------------
  local crc=0
  python3 "$RJ" contract "$OUTFILE" >/dev/null 2>&1 || crc=$?
  if [[ $crc -ne 0 ]]; then
    # generator contract failed → keep the unjudged baseline + a banner.
    python3 "$RJ" banner "$OUTFILE" "generator-block-missing" >/dev/null 2>&1 || true
    return 0
  fi

  JUDGE_PROMPT=$(mktemp /tmp/goodfellow-judgeprompt-XXXXXX)
  JUDGE_OUT=$(mktemp /tmp/goodfellow-judgeout-XXXXXX)
  local hunks="NONE"
  # The judge's diff boundary is the RAW diff (HUNKS_TMP), never the enriched
  # CTX_TMP (which now also carries D2 full files + the D1 digest).
  { has_hunks && [[ -s "$HUNKS_TMP" ]]; } && hunks="$HUNKS_TMP"
  local reason=""
  if python3 "$RJ" judge-prompt "$OUTFILE" "$CTX_TMP" "$hunks" "$JUDGE_PROMPT" 2>/dev/null; then
    local jrc=0
    timeout "$STAGE_TIMEOUT" codex exec --sandbox read-only "${model_args[@]}" \
      -o "$JUDGE_OUT" - < "$JUDGE_PROMPT" >>"$LOG" 2>&1 || jrc=$?
    if [[ $jrc -eq 0 && -s "$JUDGE_OUT" ]]; then
      local rrc=0
      python3 "$RJ" reconcile "$OUTFILE" "$JUDGE_OUT" "$OUTFILE" "${OUTFILE}.judge-audit.jsonl" || rrc=$?
      [[ $rrc -ne 0 ]] && reason="judge-parse-failure"
    else
      reason="judge-timeout"
    fi
  else
    reason="judge-setup-failed"
  fi
  if [[ -n "$reason" ]]; then
    python3 "$RJ" banner "$OUTFILE" "$reason" >/dev/null 2>&1 || true
  fi
  rm -f "$JUDGE_PROMPT" "$JUDGE_OUT" 2>/dev/null || true
  return 0
}

run_claude_fallback() {
  build_context
  local base_prompt="Review this $KIND. Focus on contradictions, undefined behavior, missing requirements, ambiguous criteria."
  [[ -n "$INCLUDE_AESTHETIC" ]] && base_prompt="$base_prompt Include aesthetic/style findings."
  [[ -n "$PROMPT" ]] && base_prompt="$PROMPT"

  local full_prompt
  full_prompt="You are an adversarial $KIND reviewer.

$base_prompt

Here is the content to review:
\`\`\`
$(cat "$CTX_TMP")
\`\`\`

Output: ## Verdict / ## Blockers / ## Major / ## Minor. Per-finding: cite section, explain issue, state fix.

NOTE: this single-model fallback is UNJUDGED (no second grounding pass)."

  if ! command -v claude &>/dev/null; then
    echo "ERROR: Neither Codex nor Claude CLI available" >&2
    FAIL_CLASS="no-reviewer-available"; exit 6
  fi

  # Capture and validate the reviewer's output BEFORE writing the wrapper banner.
  # Writing the banner into $OUTFILE up-front would make the artifact non-empty
  # even when the reviewer produced nothing — a banner-only artifact reads
  # downstream as a clean review (silent LGTM), defeating the empty-output guard.
  local rc=0 reviewer_out
  reviewer_out=$(echo "$full_prompt" | timeout "$STAGE_TIMEOUT" claude --print --model "$MODEL" 2>/dev/null) || rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "ERROR: Claude fallback reviewer failed" >&2
    FAIL_CLASS="claude-fallback-failed"; exit 5
  fi
  if ! printf '%s' "$reviewer_out" | grep -q '[^[:space:]]'; then
    echo "ERROR: Claude fallback reviewer produced empty output" >&2
    FAIL_CLASS="empty-output"; exit 4
  fi
  {
    echo "--- REVIEWER (single-model fallback, model: $MODEL) — UNJUDGED ---"
    printf '%s\n' "$reviewer_out"
  } > "$OUTFILE"
}

if has_codex; then
  run_codex
else
  run_claude_fallback
fi

# An empty/whitespace-only artifact would read downstream as "no findings" — a
# silent LGTM — so treat it as a failed review, not a clean one.
if [[ ! -s "$OUTFILE" ]] || ! grep -q '[^[:space:]]' "$OUTFILE"; then
  echo "ERROR: review produced empty output" >&2
  FAIL_CLASS="empty-output"; exit 4
fi

echo "$OUTFILE"
