---
name: snapshot
description: Extract learnings from the session before context compaction — preserves knowledge that would otherwise be lost when the context window is compressed.
---

Extract learnings before compacting context.

## Initialize and scan

At steps 0–1 read `learnings.md` in this skill's directory, initialize state and scan the current session.
Capture uncaptured principles, working patterns and surprising gotchas.
Keep the scope to learnings from this session.

## Persist knowledge

At step 2 follow the backend resolution and persistence commands in `learnings.md` in this skill's directory.
Invalid `GOODFELLOW_MEMORY` hard-errors; do not silently choose a backend.
Flat mode appends dated pending entries to `.goodfellow/knowledge.md` by section.
Create the three section headers when the flat file is absent.
Rich mode skips restatements of shipped principles and cites their P-NNN ids.
A dedup error stops; it must never silently persist a restatement.
Write kept facts with pending status through the locked memory CLI.
The CLI handles first-write migration; preserve its commands and valid fact schema.

## Snapshot enforced guards

At step 3 read `guards.md` in this skill's directory before taking the guard snapshot or compacting.
Run the selfcheck into `.goodfellow/guard-set.pre-compact.json`.
Inspect `config_error`; fix any invalid project config before compaction.
A broken config means project rules are not enforced, so do not continue past it.
The snapshot records rules, digests, built-ins, branches and hook registration.
Do not rely on a prose summary to preserve governance.

## Compact

At step 4, only after knowledge persistence and a valid guard snapshot, compact.
Report the number of extracted learnings and their knowledge destination.
Keep the snapshot on disk for the first turn after the boundary.

## Assert after the boundary

On the first post-compaction turn, follow step 5 in `guards.md` in this skill's directory verbatim.
Assert the current guard set against the saved snapshot before further work.
A changed rule, new config error, missing registration or other mismatch stops the run.
Do not swallow the assertion status with `|| echo`; failure is a gate, not a warning.
Only report the guard set intact after the assertion passes.
