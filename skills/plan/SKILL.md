---
name: plan
description: Write an implementation plan from a spec — exhaustive task decomposition with dependency graph, acceptance criteria, and spec-coverage verification. Auto-dispatches plan-review.
---

Write a plan for: $ARGUMENTS

## 1. Read the spec

Read the spec file fully. Also read `.goodfellow/knowledge.md` (Principles section) if it exists.

Also read the plugin-shipped universal design principles, so the per-task principles pass (step 4) can cite violations by `P-NNN` (the web supplement is read only when web context is opted in — `GOODFELLOW_PRINCIPLES_WEB=1` or a `package.json` at the project root; an invalid value hard-errors here):

```bash
# Capture + check exit BEFORE iterating: `for f in $(failing-cmd)` exits 0 with zero
# iterations, which would silently swallow an invalid GOODFELLOW_PRINCIPLES_WEB. Propagate it.
principle_files=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/principles_context.py" --project-root .) || { echo "$principle_files" >&2; exit 1; }
for f in $principle_files; do
  cat "${CLAUDE_PLUGIN_ROOT}/knowledge/$f"
done
```

## 2. Clarifying questions (max 3, asked once)

Only questions whose answers change execution order or task decomposition. Skip questions answerable from the spec + codebase.

**Autopilot:** skip questions entirely.

## 3. Write the plan

Write the complete plan in one pass at `docs/plans/<slug>-plan.md`.

**Required frontmatter:**
```yaml
---
title: "<Plan Title>"
spec: <path to spec file>
date: YYYY-MM-DD
---
```

**Required header format:**
- Phase headers: `## Phase N — <title>` (N is a positive integer, sequential)
- Task headers: `### T-N.X: <title>` (N = phase number, X = task index)

**Include:**
- Dependency graph (what blocks what, what parallelizes)
- Acceptance criteria per task
- Spec-coverage map (every spec section → plan task)
- Effort estimates per phase

**Scope bias: exhaustive.** Enumerate every task the spec implies. Don't truncate to look simpler.

**Principles pass:** for each task, check: does the proposed implementation introduce a principle violation per `.goodfellow/knowledge.md`? Fix in-spec or note as deliberate exception.

## 4. Self-review

- Grep clean: no placeholders, every code step has concrete content
- Spec-coverage: every spec section has at least one plan task
- Internal consistency: dependency graph matches task bodies

## 5. Auto-dispatch plan-review

After writing + self-review, in the same turn:
1. Emit summary (file path, task count, integration risks)
2. Dispatch `/goodfellow:plan-review <plan-path>`

No gate. The operator reviews through plan-review, not by approving the plan directly.

**Execution footer:**
> Use `/goodfellow:execute <plan-path>` to implement this plan.
