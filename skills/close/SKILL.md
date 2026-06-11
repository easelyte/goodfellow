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

Resolve the backend mode first (invalid `GOODFELLOW_MEMORY` hard-errors here):

```bash
MODE=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory_config.py" resolve-mode) || { echo "$MODE"; exit 1; }
```

### 2a. Extract remaining session learnings
Scan the session's recent work for principles, patterns, or gotchas not yet captured.

**flat mode (`MODE=flat`, default — behavior unchanged):** Append each learning to `.goodfellow/knowledge.md` with `[pending]` tag and date.

**rich mode (`MODE=rich`):** Before persisting a learning, skip it if it restates a shipped principle (cite the `P-NNN`):

```bash
PID=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/dedup_principles.py" --description "<learning text>" \
        --principles "${CLAUDE_PLUGIN_ROOT}/knowledge/principles.md" "${CLAUDE_PLUGIN_ROOT}/knowledge/principles-web.md")
# if $PID is non-empty: skip persisting, log "skipped (restates $PID)"
```

For each kept learning, write a per-fact file with `status: pending` (the CLI auto-migrates `knowledge.md` on first rich write):

```bash
# Valid as written — substitute your own values. --name is a kebab-slug matching
# [a-z0-9-]; --type is one of principle|pattern|gotcha; --domain is optional (omit if none):
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory_index.py" --root .goodfellow write-fact \
  --name validate-at-boundary --description "Always validate at the boundary" \
  --type principle --status pending --opened "$(date +%F)" --body "Detail of the learning."
```

### 2b. Promote pending entries

**flat mode:** Read `.goodfellow/knowledge.md`. For each `[pending]` entry:
- If the learning survived the chain (the code it references is still present and working), promote: remove `[pending]` tag
- If the learning was reverted or invalidated, remove the entry

**rich mode:** for each `status: pending` per-fact file in `.goodfellow/memory/` (substitute the real fact name for `<name>`; both commands take the lock + regenerate the index themselves, so no separate rebuild step):
- If the learning survived the chain, promote it: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory_index.py" --root .goodfellow promote --name <name>`
- If invalidated, delete it via the locked CLI (NOT a raw `rm` — that bypasses the lock and can race a concurrent write): `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory_index.py" --root .goodfellow delete-fact --name <name>`

### 2c. Pending-fact staleness (rich mode)
Flag `status: pending` per-fact files whose `opened:` date is older than the 30-day loop-staleness window for review, so orphaned pending facts (sessions ended without `/close`) don't accumulate unbounded.

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
