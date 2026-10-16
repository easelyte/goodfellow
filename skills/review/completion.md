## 5. Convergence

Converged when new findings drop to polish-tier. **Hard cap: 6 rounds**, a terminal state
`resolved | limit_reached` (P-079). Reaching the cap is a limit, never convergence:

- Safety-critical findings remain → `limit_reached`: halt, recommend a rewrite.
- Only non-blocking findings → `limit_reached`: stop and report the deferred findings as a limit,
  NOT as convergence.
- No findings → `resolved`.

At `resolved`, promote `confidence:` in the frontmatter if review settled the open architectural
questions. If the frontmatter carries pending-review keys (`review_status: pending`,
`failed_reviewers`, `resume`, written by `grill`), remove them now; keep them on any
other outcome so a later session can resume.

## 6. After the loop

Report honestly (P-079): "Spec converged at round N. Key changes: …" or "Plan review halted at
hard cap (round N); deferred: …". Never present a cap-halt as full resolution. Report research as
"supporting sources for X/Y claims (relevance-matched, not adjudicated)", never as "verified".

**Terminal safety gate (P-079).** If §5 ended in a safety-critical cap-halt, STOP. For a spec: do
NOT discard the findings and do NOT auto-dispatch plan; under autopilot append
`{"event": "spec_review_halt", "reason": "safety-critical findings remain at hard cap", "spec": "<path>"}`
to `$RUN_LOG`. For a plan: do NOT discard the findings and do NOT auto-dispatch execute; under
autopilot append `{"event": "plan_review_halt", "reason": "safety-critical findings remain at hard cap", "plan": "<path>"}`.
Surface the findings and recommend a rewrite. A known-unsafe document must not advance.

Otherwise (genuine convergence, or a cap-halt with only non-blocking findings), carry the residue
forward so nothing silently disappears: file each remaining **major** as a loop
(`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/loop_store.py" --root . add "<title>" --priority p3 --source "review-r<N>" --description "<finding>"`)
and list it in the hand-off summary; remaining minors are dropped. (Dry-run: log each as a
`would_file_loop` event instead.)
On a safety-critical cap-halt, do NOT discard them; the gate above preserves and surfaces them.

**Halt key.** If the frontmatter says `next_action: halt-after-spec-review`, stop after the spec
review and say: "Spec converged at round N. Frontmatter requests `halt-after-spec-review`; run
`/goodfellow:plan <path>` when ready." Under autopilot also append
`{"event": "halt_after_spec_review", "spec": "<path>"}`.

**Hand-off.** Spec mode: dispatch `/goodfellow:plan <spec-path>`. Plan mode: dispatch
`/goodfellow:execute <plan-path>`.
