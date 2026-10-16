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
Offer to run `/goodfellow:prune` for a broader sweep.

## 5. Summary

"Session closed. Knowledge: N entries promoted, M new pending. Loops: X open (Y stale). Branch: <status>."
