## 2. Clarifying questions (max 3, asked once)

Only questions whose answers change execution order or task decomposition. Skip questions answerable from the spec + codebase.

**Autopilot (default):** ask only product calls; decide the rest and note them in the plan.

## 3. Write the plan

Write the complete plan in one pass at `docs/plans/<slug>-plan.md`.

**Required frontmatter:**
```yaml
---
title: "<Plan Title>"
spec: <path to spec file>
tier: <T2|T3, copied from the spec>
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

**Test design per task** (P-094, P-095, P-096). Every task that adds or changes behaviour names its tests and the **expected red**: the assertion message each new test fails with before the change. "Fails with function not defined" is not a red; when the symbol is new, the first step is a stub that returns a wrong answer, so the red comes from the assertion. For tasks on **high-stakes paths** (the globs in `.goodfellow/high_stakes_paths.txt` if you keep one; otherwise allow/deny, gates, deletion and retention, money, alert or verdict logic), also name:
- the fail-closed error branches and exact boundaries the tests pin, with a case on each side of each edge;
- one deliberate break per rule that the tests must catch (flip the comparison, return the allow verdict from the error branch, drop the raise);
- for code that signals processes, deletes or writes real resources: the fake or isolated namespace (for example `unshare --pid`) its tests and breaks run in. Such code is never tested or mutated against the live machine;
- the real entry point the tests drive. A source-text check may accompany a behaviour test, never replace one.

**T3: a rehearsal task, and a live step that is not a task.** A T3 plan includes a task that
performs the real mutations (the migration, the deploy step, the deletion, the send) against a
sandbox the user supplies (a database copy, a temporary tree, a namespace) and records the evidence
for the PR. The live change itself is **not** a task `execute` runs. The plan ends with a
`## Live step` section: the exact commands, whether they run before or after the merge, how to
verify the result, and how to roll back. `ship` hands it to the operator only after the code review
has converged and CI is green, so a defect the review finds can never reach production first.

**Scope bias: exhaustive.** Enumerate every task the spec implies. Don't truncate to look simpler.

**Principles pass:** for each task, check: does the proposed implementation introduce a principle violation per `.goodfellow/knowledge.md`? Fix in-spec or note as deliberate exception.

