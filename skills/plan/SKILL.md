---
name: plan
description: Write an implementation plan from a spec — exhaustive task decomposition with dependency graph, acceptance criteria, and spec-coverage verification. Auto-dispatches plan-review.
---

Write a plan for: $ARGUMENTS

## 1. Read the spec

Read the spec file fully. Also read `.shipline/knowledge.md` (Principles section) if it exists.

## 2. Clarifying questions (max 3, asked once)

Only questions whose answers change execution order or task decomposition. Skip questions answerable from the spec + codebase.

**Autopilot:** skip questions entirely.

## 3. Write the plan

Write the complete plan in one pass at `docs/plans/<slug>-plan.md`.

**Required header format:**
- Phase headers: `## Phase N — <title>` (N is a positive integer, sequential)
- Task headers: `### T-N.X: <title>` (N = phase number, X = task index)

**Include:**
- Dependency graph (what blocks what, what parallelizes)
- Acceptance criteria per task
- Spec-coverage map (every spec section → plan task)
- Effort estimates per phase

**Scope bias: exhaustive.** Enumerate every task the spec implies. Don't truncate to look simpler.

**Principles pass:** for each task, check: does the proposed implementation introduce a principle violation per `.shipline/knowledge.md`? Fix in-spec or note as deliberate exception.

## 4. Self-review

- Grep clean: no placeholders, every code step has concrete content
- Spec-coverage: every spec section has at least one plan task
- Internal consistency: dependency graph matches task bodies

## 5. Auto-dispatch plan-review

After writing + self-review, in the same turn:
1. Emit summary (file path, task count, integration risks)
2. Dispatch `/shipline:plan-review <plan-path>`

No gate. The operator reviews through plan-review, not by approving the plan directly.

**Execution footer:**
> Use `/shipline:execute <plan-path>` to implement this plan.
