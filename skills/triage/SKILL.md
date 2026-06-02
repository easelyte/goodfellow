---
name: triage
description: Two-reviewer loop triage — independently assess each open loop, reconcile verdicts, batch operator confirmation. 3-cycle hard cap on unclear findings. Ground truth logged to triage-log.jsonl.
---

Triage open loops to separate real defects from noise.

## 1. Load open loops

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/loop_store.py" --root . list
```

If no open loops: "No open loops to triage." Exit.

## 2. Per-loop two-reviewer assessment

For each open loop:

### Reviewer 1 (Claude subagent)
Dispatch an Agent subagent:
> "Assess this follow-up: '<loop title>'. Description: '<loop description>'. Source: '<loop source>'. Check the cited code surface and recent git history. Is this finding still real? Respond with exactly one of: `real-defect`, `not-a-defect`, `still-unclear`. Include a 1-2 sentence reason."

### Reviewer 2 (Codex bridge or Claude fallback)
```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/codex-bridge.sh" --kind diff --uncommitted -- "Assess: <loop title>. <loop description>. Real, not-real, or unclear?"
```

Both are independent — neither sees the other's output.

## 3. Reconciliation

| Reviewer 1 + Reviewer 2 | Result | Confidence |
|---|---|---|
| Both agree | That tag | high |
| One opinion, other unclear | Opinion wins | medium |
| Disagree | still-unclear | low |
| Both unclear | still-unclear | low |

## 4. Hard cap check

If this loop's `triage_count` is already 2 and the result is `still-unclear`:
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
- `still-unclear` → increment triage_count, update last_triaged

## 7. Log to ground truth

Append each decision to `.goodfellow/triage-log.jsonl` (lock + flush + fsync, truncated-line tolerant):

```json
{"loop_id": 1, "title": "...", "decision": "real-defect", "confidence": "high", "reviewer_1": "real-defect", "reviewer_2": "real-defect", "date": "2026-06-02", "operator_override": false}
```

## 8. Summary

"Triaged N loops: X real-defect, Y not-a-defect, Z still-unclear. M loops at MUST DECIDE threshold."
