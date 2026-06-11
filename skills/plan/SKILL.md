---
name: plan
description: Write an implementation plan from a spec — exhaustive task decomposition with dependency graph, acceptance criteria, and spec-coverage verification. Auto-dispatches plan-review.
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
# One robust command: resolves + reads the seeded principles, with ALL error handling
# in Python (bad config / missing core / unreadable file -> non-zero exit + stderr).
# Its stdout IS the principles to apply (cite violations by P-NNN). A non-zero exit
# means a config/packaging problem — stop and surface it.
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/principles_context.py" --emit --project-root .
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
