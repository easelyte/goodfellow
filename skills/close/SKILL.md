---
name: close
description: Structured session closing — commit check, persist learnings to knowledge file, promote pending entries, check loop staleness, branch cleanup.
---

Close the current session cleanly.

## 0. Ensure state directory

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/init_state.sh"
```

## 1. Uncommitted changes check

Run `git status`. If uncommitted changes exist, warn and offer to commit.

## 2. Knowledge persistence

### 2a. Extract remaining session learnings
Scan the session's recent work for principles, patterns, or gotchas not yet captured. Append to `.goodfellow/knowledge.md` with `[pending]` tag and date.

### 2b. Promote pending entries
Read `.goodfellow/knowledge.md`. For each `[pending]` entry:
- If the learning survived the chain (the code it references is still present and working), promote: remove `[pending]` tag
- If the learning was reverted or invalidated, remove the entry

## 3. Loop staleness check

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/loop_store.py" --root . stale
```

Flag open loops older than 30 days. Present the list.

### Soft cap warning
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/loop_store.py" --root . count
```

If >15 active loops: "Loop backlog at N — consider running /goodfellow:triage before filing more."

## 4. Branch cleanup

If on a feature branch that's been merged, offer to delete it.
Offer to run `/goodfellow:prune-stale` for a broader sweep.

## 5. Summary

"Session closed. Knowledge: N entries promoted, M new pending. Loops: X open (Y stale). Branch: <status>."
