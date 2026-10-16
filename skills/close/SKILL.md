---
name: close
description: Structured session closing — commit check, persist learnings to knowledge file, promote pending entries, check loop staleness, branch cleanup.
---

Close the current session cleanly.

## Initialize and inspect

At steps 0–1 read `learnings.md` in this skill's directory, initialize state, then run git status.
Warn about uncommitted changes and offer to commit them.
Keep unrelated work visible when deciding how to close the session.

## Persist remaining learnings

At step 2 read the rest of `learnings.md` in this skill's directory before changing knowledge state.
Resolve the backend first; invalid `GOODFELLOW_MEMORY` hard-errors.
Scan for principles, patterns and gotchas not yet captured.
Flat mode appends dated pending entries to `.goodfellow/knowledge.md`.
Rich mode deduplicates against shipped principles, citing the matching P-NNN.
A dedup error stops; never persist a restatement after a failed check.
Write kept facts through the documented memory CLI with pending status.
The first rich write migrates the old knowledge file through the CLI.

## Promote or remove pending entries

At step 2b follow the separate flat and rich instructions in `learnings.md` in this skill's directory.
Promote only learnings whose referenced code survived and still works.
Remove invalidated or reverted learnings.
Rich facts use locked promote/delete commands; raw deletion can race a writer.
Do not bypass the CLI's locking and index regeneration.
At step 2c flag rich pending facts older than 30 days for review.

## Check loops

At step 3 read `cleanup.md` in this skill's directory and execute its loop listing command.
Flag stale open loops using the documented 30-day window.
Apply the soft backlog warning and suggest triage when its threshold is reached.
Stale work must remain visible rather than silently disappearing at session close.

## Clean up and report

At steps 4–5 continue with `cleanup.md` in this skill's directory for branch cleanup and the final summary.
If the current feature branch has been merged, offer to delete it.
Offer `/goodfellow:prune` for a broader sweep.
Report knowledge promotions, removals, loop staleness and cleanup outcomes honestly.
