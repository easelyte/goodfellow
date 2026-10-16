---
name: plan
description: Write an implementation plan from a spec — exhaustive task decomposition with dependency graph, acceptance criteria, and spec-coverage verification. Auto-dispatches review --plan.
---

Write a plan for: $ARGUMENTS

## Read inputs

At step 1 read `inputs.md` in this skill's directory before writing anything.
Read the spec fully, then backend-aware accumulated knowledge and the principles index.
Invalid memory mode or principle configuration stops execution.
Rich mode reads the full index, discounts pending facts, and pulls matching domains.
Expand relevant categories and full principles before applying or citing them.

## Clarify execution choices

At step 2 read `writing.md` in this skill's directory for clarification and complete plan requirements.
Ask at most three questions, once, only when they change order or decomposition.
Skip questions answerable from the spec or codebase.
Autopilot asks only product calls; decide the rest and record them in the plan.

## Write the whole plan

At step 3 continue with `writing.md` in this skill's directory; write `docs/plans/<slug>-plan.md` in one pass.
Use its exact frontmatter, sequential phase headers and task header format.
Include dependencies, acceptance criteria, spec coverage and phase effort estimates.
Enumerate every implied task; do not truncate scope to look simpler.
Every behavior task names tests and expected assertion reds, with wrong-answer stubs if needed.
High-stakes tasks name error boundaries, deliberate breaks, fakes and the real entry point.
Never test destructive breaks against live resources.
Check every proposed implementation against knowledge principles; fix or explain exceptions.

## Separate rehearsal from live work

When the spec is T3, apply the rehearsal and Live step requirements in `writing.md` in this skill's directory.
Rehearse the real mutations against a sandbox the user supplies and record evidence.
The live change is not an execute task: give exact commands, timing, verification and rollback.
Ship hands live work to the operator only after converged review and green CI.

## Self-review and hand off

At steps 4–5 read `handoff.md` in this skill's directory for completeness checks and dispatch syntax.
Remove placeholders, prove every spec section has a task, and check dependency consistency.
Report path, task count and integration risks, then dispatch `/goodfellow:review --plan`.
No direct approval gate; include the documented execute footer in the plan.
