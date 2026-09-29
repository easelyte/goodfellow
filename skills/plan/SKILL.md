---
name: plan
description: Write an implementation plan from a spec — exhaustive task decomposition with dependency graph, acceptance criteria, and spec-coverage verification. Auto-dispatches review-doc --plan.
---

Write a plan for: $ARGUMENTS

## 1. Read the spec

Read the spec file fully, then read the project's accumulated knowledge, backend-aware (invalid `GOODFELLOW_MEMORY` hard-errors here):

```bash
MODE=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory_config.py" resolve-mode) || { echo "$MODE"; exit 1; }
if [ "$MODE" = "rich" ]; then
  # Full MEMORY.md index (incl. ## Pending (unconfirmed) — discount those). Internally
  # falls back: .migrating -> knowledge.md (no regen), absent -> knowledge.md, dirty/stale -> regenerate.
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory_index.py" --root .goodfellow read-index
else
  cat .goodfellow/knowledge.md 2>/dev/null || true   # flat: Principles section
fi
```

In rich mode, auto-pull bodies of exact-`domain` matches; open other relevant fact bodies by name from the index.

Also read the plugin-shipped universal design principles, so the per-task principles pass (step 4) can cite violations by `P-NNN` (the web supplement is read only when web context is opted in — `GOODFELLOW_PRINCIPLES_WEB=1` or a `package.json` at the project root; an invalid value hard-errors here):

```bash
# Progressive disclosure (docs/instruction-density-budget.md): inject only the
# principle INDEX (P-NNN id + title + one-line rule), NOT the full bodies. The full
# corpus is ~17k tokens / ~300 directives — well past the accuracy-erosion ceiling.
# All error handling stays in Python (bad config / missing core / unreadable file ->
# non-zero exit + stderr); a non-zero exit means a config/packaging problem — stop.
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/principles_context.py" --index --project-root .
```

Scan the category routing table. For any category relevant to this work, expand its one-liners with
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/principles_context.py" --category NAME --project-root .`, then pull the
full body of a relevant principle with `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/principles_context.py" --show P-NNN [P-NNN ...] --project-root .`
before applying or citing it — requesting a parent id (e.g. `P-017`) includes its
sub-principles. Cite violations by P-NNN.

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

**T3: rehearsal task, then a gated live step.** A T3 plan includes a task that performs the real
mutations (the migration, the deploy step, the deletion, the send) against a sandbox the user
supplies (a database copy, a temporary tree, a namespace) and records the evidence for the PR. The
live step itself is its own final task, headed `### T-N.X: LIVE — <what it changes>`. `execute`
never runs a `LIVE` task on its own: it stops before it, shows the rehearsal evidence, and asks the
operator, whatever the autopilot setting. The stop list cannot see sends or spending in a command
line, so this gate is what protects them.

**Scope bias: exhaustive.** Enumerate every task the spec implies. Don't truncate to look simpler.

**Principles pass:** for each task, check: does the proposed implementation introduce a principle violation per `.goodfellow/knowledge.md`? Fix in-spec or note as deliberate exception.

## 4. Self-review

- Grep clean: no placeholders, every code step has concrete content
- Spec-coverage: every spec section has at least one plan task
- Internal consistency: dependency graph matches task bodies

## 5. Auto-dispatch review-doc

After writing + self-review, in the same turn:
1. Emit summary (file path, task count, integration risks)
2. Dispatch `/goodfellow:review-doc --plan <plan-path>`

No gate. The operator reviews through review-doc, not by approving the plan directly.

**Execution footer:**
> Use `/goodfellow:execute <plan-path>` to implement this plan.
