---
name: prune-stale
description: Sweep merged/stale branches, orphan worktrees, and old JSONL logs. Configurable retention via GOODFELLOW_TRIAGE_RETENTION_DAYS and GOODFELLOW_RUNS_RETENTION_DAYS.
---

Clean up stale branches, worktrees, and logs.

## 1. Merged branches

List branches already merged into main/master:

```bash
git branch --merged main | grep -v '^\*\|main\|master'
```

Present the list. Confirm before deleting each.

## 2. Orphan worktrees

```bash
git worktree list
```

Flag worktrees whose branch no longer exists or whose directory is missing.

```bash
git worktree prune
```

## 3. JSONL log retention

### Triage log
Sweep `.goodfellow/triage-log.jsonl` — remove entries for closed loops older than retention period.

Default: 90 days. Override: `GOODFELLOW_TRIAGE_RETENTION_DAYS` env var.

Entries for active loops are never pruned.

### Autopilot run logs
Sweep `.goodfellow/runs/` — delete JSONL files older than retention period.

Default: 90 days. Override: `GOODFELLOW_RUNS_RETENTION_DAYS` env var.

## 4. Knowledge file size check

If `.goodfellow/knowledge.md` exceeds 50KB:
"Knowledge file is large (NKB). Consider curating — remove outdated entries or consolidate related principles."

## 5. Summary

"Pruned: N branches, M worktrees, X triage entries, Y run logs. Knowledge: <size status>."
