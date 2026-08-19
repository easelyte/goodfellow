---
name: triage
description: Two-reviewer loop triage — independently assess each open loop, reconcile verdicts, batch operator confirmation. 3-cycle hard cap on unclear findings. Ground truth logged to triage-log.jsonl.
---

Triage open loops to separate real defects from noise.

## 0. Ensure state directory

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/init_state.sh"
```

## 1. Load open loops

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/loop_store.py" --root . list
```

If no open loops: "No open loops to triage." Exit.

## 2. Per-loop two-reviewer assessment (parallel)

Each open loop gets two independent reviewers. The reviewers are **read-only** — they assess a loop against the cited code surface + git history and return a verdict; they never write the tree. Because nothing mutates the working tree, there is **no file-collision risk**, so per-loop reviewers can run concurrently.

**Dispatch the whole backlog in parallel, not one loop at a time.** For a backlog of N loops, fire all N Reviewer-1 subagents in a single message (one `Agent`/Task tool block with N concurrent tool uses) rather than serializing N sequential dispatches.

**Soft cap — batch large backlogs into waves.** Do not launch an unbounded number of concurrent children the harness cannot supervise. If the backlog exceeds ~8-10 loops, split it into waves of ~8-10 loops each: fire one wave in parallel, wait for it to complete, then fire the next. Within a wave, all dispatches go out concurrently.

### Reviewer 1 (Claude subagent) — one per loop, dispatched concurrently
For each loop in the current wave, dispatch an Agent subagent (all in the same message):
> "Assess this follow-up: '<loop title>'. Description: '<loop description>'. Source: '<loop source>'. Check the cited code surface and recent git history. Is this finding still real? Respond with exactly one of: `real-defect`, `not-a-defect`, `still-unclear`. Include a 1-2 sentence reason."

### Reviewer 2 (Codex bridge or Claude fallback) — one per loop
```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/codex-bridge.sh" --kind diff --uncommitted -- "Assess: <loop title>. <loop description>. Real, not-real, or unclear?"
```

Both reviewers are independent — neither sees the other's output. Collect both verdicts per loop, then reconcile each loop with the table in §3 (reconciliation is unchanged; it is still per-loop).

**Failed-review contract (Reviewer 2):** on success the bridge prints an artifact path on stdout; if it exits nonzero it prints `REVIEW_FAILED <code> <class>` instead. Treat that as a FAILED assessment, never clean/LGTM — reject the `REVIEW_FAILED` prefix before any read, surface it, and skip that loop's Reviewer-2 verdict (fall back to Reviewer 1 / re-run) rather than recording an empty result as a verdict.

## 3. Reconciliation

| Reviewer 1 + Reviewer 2 | Result | Confidence |
|---|---|---|
| Both agree | That tag | high |
| One opinion, other unclear | Opinion wins | medium |
| Disagree | still-unclear | low |
| Both unclear | still-unclear | low |

## 4. Hard cap check

If this loop's `triage_count` has reached 3 (three prior unclear cycles):
- Override to **MUST DECIDE** — present the loop with both reviewers' reasoning and force the operator to choose `real-defect` or `not-a-defect`. Won't accept `still-unclear`.

## 5. Batch table presentation

Present all loops in a table:

```
# | Title | Reviewer 1 | Reviewer 2 | Reconciled | Confidence | Action
1 | Auth edge cases | real-defect | real-defect | real-defect | high | keep open
2 | Stale import | not-a-defect | not-a-defect | not-a-defect | high | close
3 | Race condition | real-defect | still-unclear | real-defect | medium | keep open
4 | Old finding | MUST DECIDE (3 unclear cycles) | | | | operator decides
```

Operator confirms, overrides, or skips each row.

## 6. Apply decisions

For each confirmed decision:
- `real-defect` → loop stays open. No changes to store.
- `not-a-defect` → close the loop:
  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/loop_store.py" --root . close <id>
  ```
- `still-unclear` → increment triage_count, update last_triaged:
  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/loop_store.py" --root . update-triage <id> --count <N+1>
  ```

## 7. Log to ground truth

Append each decision to `.goodfellow/triage-log.jsonl` (lock + flush + fsync, truncated-line tolerant):

```json
{"loop_id": 1, "loop_uuid": "<loop's uuid from loops.json>", "title": "...", "decision": "real-defect", "confidence": "high", "reviewer_1": "real-defect", "reviewer_2": "real-defect", "date": "2026-06-02", "operator_override": false}
```

Include `loop_uuid` — copy the `uuid` field of the loop from `loops.json`. It is the durable identity the lens-tuning join keys on: the integer `loop_id` restarts at 1 on any store reset, so a record carrying only `loop_id` can be mis-attributed to a different loop after a reset. `loop_uuid` is collision-proof across resets. (Legacy records without it still load; the join falls back to `loop_id`.)

## 8. Summary

"Triaged N loops: X real-defect, Y not-a-defect, Z still-unclear. M loops at MUST DECIDE threshold."

## 9. Optional: reviewer-lens tuning (read-only pointer)

Once triage decisions accumulate, surface review `source`s worth a human look — those whose surviving deferred findings were mostly triaged `not-a-defect` (rejection signal) or mostly operator-overridden (disagreement signal). It joins `loops.json` + `triage-log.jsonl`, attributes at `source` granularity (the only durable proxy — no record stores a lens tag), and points at the lens prose. Read-only; it never edits, and a human validates before tuning.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/lens_tuning.py" --root .        # human report
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/lens_tuning.py" --root . --json  # machine-readable
```

Read the report's caveats before acting. The ratio is a **deferred-loop disposition, NOT the lens false-positive rate**: only findings deferred to loops are counted (findings fixed inline during review, and polish-tier deferred findings filed as gotchas, never enter the stores), so do not weaken a lens on this signal alone. Rejection counts are a **floor** — `not-a-defect` closes a loop and retention prunes old closed-loop entries while real-defect loops persist, so pruned rejections are under-reported. `operator_override` is direction-less (disagreement, not proven noise). Flags require a minimum sample (`--min-sample`, default 3), so one decision never triggers one. Under-firing (missed defects) is out of scope.
