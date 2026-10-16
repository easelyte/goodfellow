---
name: execute
description: Per-task plan implementation with built-in verification (lint, format, tests) after each task, knowledge gotcha checking, and optional phase-boundary Codex review. Runs all tasks without pausing unless autopilot is off (GOODFELLOW_AUTOPILOT=0).
---

Implement the plan at: $ARGUMENTS

## Load inputs

Read `inputs.md` in this skill's directory at steps 0–1: check worktree hygiene, read the full plan and knowledge.
The root-workspace warning is advisory; it does not block execution.
Invalid memory/principle configuration stops; expand principles before applying them.
Read all fact categories in rich mode, discount pending entries, and check gotchas.

## Choose serial or isolated parallel work

Default to serial tasks in plan order; dependencies must be complete first.
Only when considering fan-out at step 2.0, read `parallel.md` in this skill's directory before checkpointing.
State the fan-out count and reason; fewer than about 3 independent tasks stays serial.
Never exceed runtime capacity. Checkpoint explicit owned paths and audit the index.
Each child needs runtime confinement and the checkpoint SHA verified before writes.
Use the documented degraded fallback only within its trust conditions; otherwise run serial.
Quarantine a child whose termination is unconfirmed; never force-remove its worktree.
Never replay external non-idempotent effects while the first execution may still be live.
Merge in plan order and verify all changes and acceptance criteria together.

## Implement each task

At steps 2a–2e, read `tasks.md` in this skill's directory before the first task and follow it for every task.
Read task and dependencies, surface matching gotchas, then prove assertion red before green.
Missing symbols are not red evidence; stub a wrong answer and observe an assertion.
Never change expectations merely to match output; explain any justified correction.
High-stakes deliberate breaks run against fakes or isolated resources and are restored safely.
Record red and deliberate-break evidence; verify before marking a task complete.
Failures must be fixed before continuing. Autopilot off pauses at phase boundaries.
T3 live steps never execute here; a task that mutates live state stops for the operator.

## Review phases and finish

At step 3, read `completion.md` in this skill's directory for optional phase review and its bridge command.
Reject `REVIEW_FAILED` and any nonzero review exit; fix blockers before continuing.
At step 4 follow its terminal gate (P-079): all tasks and required checks must pass.
Only that success path dispatches `/goodfellow:ship`.
A stopped, partial, limited or unverified run reports “Execution halted” and HALTs the chain.
Follow-up filing cannot replace the halt; enumerate remaining work and unrun checks.
