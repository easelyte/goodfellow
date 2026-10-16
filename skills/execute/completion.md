## 3. Phase-boundary review (optional)

At the end of each phase, optionally run a quick review:

```bash
OUT=$(bash "${CLAUDE_PLUGIN_ROOT}/scripts/codex-bridge.sh" --kind diff --uncommitted) || {
  echo "review bridge failed: $OUT" >&2; exit 1; }
case "$OUT" in REVIEW_FAILED\ *) echo "review bridge failed: $OUT" >&2; exit 1 ;; esac
# On success $OUT is the review-artifact path (Codex path is judged; see its
# `## Judge audit` section). Reject the REVIEW_FAILED sentinel before reading.
```

**Failed-review contract:** if the bridge exits nonzero it prints `REVIEW_FAILED <code> <class>` instead of an artifact path. Treat that as a FAILED review, never clean/LGTM — reject the `REVIEW_FAILED` prefix before any read, surface it, and stop; do not treat a failed review as a clean phase boundary.

Surface any findings. Fix blockers before proceeding to next phase.

Report: "Phase N complete. M tasks done. Continuing to Phase N+1." (With `GOODFELLOW_AUTOPILOT=0`, wait for a go.)

## 4. After all tasks

Summarize honestly (P-079 — reaching a limit is not success). Reaching the end of the task
list is not the same as completing the work, so the *whole* summary is conditional — do not
lead with a success claim you then walk back:

- **Success path** — every task completed AND all required verification ran and passed:
  "Execution complete. N tasks across M phases. Verification passed."
- **Partial/halted path** — any task stopped at a blocker or limit, or any verification was
  skipped, partial, or failing: "Execution halted after K of N tasks." Enumerate the
  remaining tasks and any unrun/failed verification (e.g. "Stopped at T-x.y: <reason>";
  "Verification skipped for T-x.y"). Never emit "Execution complete" on this path.

**Terminal gate before shipping (P-079).** Dispatch ship ONLY on the success path above. On
the partial/halted path, do NOT auto-dispatch ship — HALT and surface the incomplete state
to the operator. Under autopilot, stop the chain here rather than cascading; do not ship
partial or unverified work. Filing the gaps as follow-ups is not a substitute for the halt.

Otherwise (success path) auto-dispatch `/goodfellow:ship`.
