## 9. Optional: reviewer-lens tuning (read-only pointer)

Once triage decisions accumulate, surface review `source`s worth a human look — those whose surviving deferred findings were mostly triaged `not-a-defect` (rejection signal) or mostly operator-overridden (disagreement signal). It joins `loops.json` + `triage-log.jsonl`, attributes at `source` granularity AND per-`lens` for loops carrying the judge's lens tag, and points at the lens prose. Loops filed before the tag shipped have no lens and appear under `unattributed` (never flagged). Read-only; it never edits, and a human validates before tuning.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/lens_tuning.py" --root .        # human report
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/lens_tuning.py" --root . --json  # machine-readable
```

Read the report's caveats before acting. The ratio is a **deferred-loop disposition, NOT the lens false-positive rate**: only findings deferred to loops are counted (findings fixed inline during review, and polish-tier deferred findings filed as gotchas, never enter the stores), so do not weaken a lens on this signal alone. Rejection counts are a **floor** — `not-a-defect` closes a loop and retention prunes old closed-loop entries while real-defect loops persist, so pruned rejections are under-reported. `operator_override` is direction-less (disagreement, not proven noise). Flags require a minimum sample (`--min-sample`, default 3), so one decision never triggers one. Under-firing (missed defects) is out of scope.
