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
{"loop_id": 1, "loop_uuid": "<loop's uuid from loops.json>", "title": "...", "decision": "real-defect", "confidence": "high", "reviewer_1": "real-defect", "reviewer_2": "real-defect", "date": "2026-06-02", "operator_override": false, "lens": "<loop's lens, or omit if none>"}
```

Include `loop_uuid` — copy the `uuid` field of the loop from `loops.json`. It is the durable identity the lens-tuning join keys on: the integer `loop_id` restarts at 1 on any store reset, so a record carrying only `loop_id` can be mis-attributed to a different loop after a reset. `loop_uuid` is collision-proof across resets. (Legacy records without it still load; the join falls back to `loop_id`.)

## 8. Summary

"Triaged N loops: X real-defect, Y not-a-defect, Z still-unclear. M loops at MUST DECIDE threshold."

