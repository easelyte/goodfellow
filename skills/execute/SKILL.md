---
name: execute
description: Per-task plan implementation with built-in verification (lint, format, tests) after each task, knowledge gotcha checking, and optional phase-boundary Codex review. Autopilot mode runs all tasks without pausing.
---

Implement the plan at: $ARGUMENTS

## 0. Worktree hygiene check

Before starting execution, check if you're running in the root workspace:

```bash
git rev-parse --show-toplevel
git worktree list
```

If the current directory IS the root workspace (not a worktree), warn:

> "Running in root workspace. For cleaner isolation (especially on Windows where Codex temp folders require admin rights to delete), consider `/goodfellow:branch <topic>` first, then execute from the worktree."

Proceed regardless — this is a nudge, not a gate.

## 1. Read the plan and knowledge

Read the plan file. Parse phases and tasks (headers: `## Phase N`, `### T-N.X`).

Read `.goodfellow/knowledge.md` Gotchas section if it exists — these are known footguns to watch for during implementation.

Also read the plugin-shipped universal design principles and apply them at the code-writing stage (the web supplement is read only when web context is opted in — `GOODFELLOW_PRINCIPLES_WEB=1` or a `package.json` at the project root; an invalid value hard-errors here):

```bash
for f in $(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/principles_context.py" --project-root .); do
  cat "${CLAUDE_PLUGIN_ROOT}/knowledge/$f"
done
```

## 2. Per-task implementation loop

For each task in plan order:

### 2a. Read the task
Read the task body, acceptance criteria, and dependencies. Check that dependencies are complete.

### 2b. Check gotchas
Scan the task's target files/modules against knowledge gotchas. If a gotcha matches, surface it before implementing: "Knowledge gotcha: <entry>. Accounting for this in implementation."

### 2c. Implement
Write the code/config/docs the task specifies. Follow acceptance criteria.

### 2d. Verify
After implementation, run verification:

**Auto-detect toolchain:**
- Python files changed → `ruff check` + `ruff format --check` (if ruff installed)
- JS/TS files changed → `eslint` or project's configured linter (if installed)
- JSON files changed → `python3 -c "import json; json.load(open('<file>'))"` structural validation
- Test files matching changed modules → discover and run them

If verification fails: fix the issue before proceeding. Do not silently continue.

### 2e. Mark complete
Note the task as done. Proceed to next task.

**Autopilot:** proceed through all tasks without pausing. Report progress at phase boundaries.

## 3. Phase-boundary review (optional)

At the end of each phase, optionally run a quick review:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/codex-bridge.sh" --kind diff --uncommitted
```

Surface any findings. Fix blockers before proceeding to next phase.

In interactive mode, pause briefly: "Phase N complete. M tasks done. Continuing to Phase N+1."

## 4. After all tasks

Summarize: "Execution complete. N tasks across M phases. Verification passed."

Auto-dispatch `/goodfellow:ship`.
