## 2. Review

### The review loop (every tier)
Multi-round adversarial review on the diff. Same convergence algorithm as review:
- Two reviewers per round (Claude + Codex/single-Claude fallback via bridge)
- Verifier pass at round 2+ (via `convergence_detector.py`)
- Research injection between rounds 1 and 2 if factual claims in findings
- Convergence when severity drops to polish-tier
- Hard cap: 3 rounds at T0 and T1, 6 at T2 and T3 (a cost bound; convergence, not the cap, ends a
  healthy loop). A diff that changes only docs or skill text (no code, config or tests) is capped at
  one round plus one delta review at any tier: each prose fix tends to invite a new edge case.

```bash
# Review exactly what the PR will contain: commit the work first (stage files by name,
# including new ones), then review the branch against BASE.
[ -z "$(git status --porcelain)" ] || { echo "uncommitted changes: commit them before review" >&2; exit 1; }
git diff --quiet "$BASE"...HEAD && { echo "nothing to ship against $BASE" >&2; exit 1; }
OUT=$(bash "${CLAUDE_PLUGIN_ROOT}/scripts/codex-bridge.sh" --kind diff --base "$BASE") || {
  echo "review bridge failed: $OUT" >&2; exit 1; }
case "$OUT" in REVIEW_FAILED\ *) echo "review bridge failed: $OUT" >&2; exit 1 ;; esac
# On success $OUT is the review-artifact path; the Codex path is judged (see its
# `## Judge audit` section). Reject the REVIEW_FAILED sentinel before reading —
# never treat a failed review as an empty (zero-findings) pass.
```

A review whose artifact shows an empty diff while the branch differs from `BASE` is a failed
review, not a clean one.

**Failed-review contract:** if the bridge exits nonzero it prints `REVIEW_FAILED <code> <class>` instead of an artifact path. Treat that as a FAILED review, never clean/LGTM — reject the `REVIEW_FAILED` prefix before any read, surface it, and stop (do NOT proceed to PR/merge). A failed review is not a passed one.

### Final-HEAD check

The loop ends by *fixing* the last round's findings, so the last fix commit has been reviewed by
nobody. Before the PR is opened or merged, if HEAD moved after the last reviewed state, run one
narrow review of just the fix commits. If you rebased since that review, the reviewed SHA is no
longer an ancestor of HEAD, so map it first:

```bash
# Prints "<sha> <mode> <reason>". ancestor/rebase: review <sha>...HEAD. reviewed: nothing new
# (HEAD is the reviewed commit or its exact rebase). full: review the whole branch.
# Exit 2 (git failed): review the whole branch against BASE.
read -r DELTA MODE REASON < <(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/review_delta.py" \
  --last <last-reviewed-sha> --base "$BASE") || { DELTA=$BASE MODE=full; }
echo "final-HEAD check: $MODE ($REASON)"
[ "$MODE" = reviewed ] || {
  OUT=$(bash "${CLAUDE_PLUGIN_ROOT}/scripts/codex-bridge.sh" --kind diff --base "$DELTA") || {
    echo "review bridge failed: $OUT" >&2; exit 1; }
  case "$OUT" in REVIEW_FAILED\ *) echo "review bridge failed: $OUT" >&2; exit 1 ;; esac
}
```

The mapping matches the reviewed commits to the rebased ones by patch-id and requires the reviewed
files to be byte-identical; a dropped, reordered or rewritten reviewed commit, or a merge commit in
the branch, reviews the whole branch. Unmapped is unreviewed. A `full` result is still this gate,
not a new round.

It asks one question: did the fix introduce a defect? Only a blocker or major inside those commits
halts; anything else is recorded, not fixed here. It is a gate, not a round, and runs at most twice.

## 3. Ship-blocking check

**If any unresolved safety-critical finding remains at convergence or hard cap: HALT.** No PR creation, no merge. The finding must be fixed or the operator must explicitly waive it.

Filing as a loop is NOT sufficient for safety-critical findings at ship time.

