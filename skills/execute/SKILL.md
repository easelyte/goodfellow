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

Read the project's accumulated knowledge, backend-aware (invalid `GOODFELLOW_MEMORY` hard-errors here):

```bash
MODE=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory_config.py" resolve-mode) || { echo "$MODE"; exit 1; }
if [ "$MODE" = "rich" ]; then
  # execute reads the FULL MEMORY.md index in rich mode (NOT a gotchas-only subset —
  # that would silently drop confirmed pattern/principle facts); it WEIGHTS gotchas/
  # principles at the code-writing stage. Includes ## Pending (unconfirmed) — discount those.
  # Internal fallback: .migrating -> knowledge.md (no regen), absent -> knowledge.md, dirty/stale -> regenerate.
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory_index.py" --root .goodfellow read-index
else
  cat .goodfellow/knowledge.md 2>/dev/null || true   # flat: Gotchas are known footguns to watch for
fi
```

In rich mode, auto-pull bodies of exact-`domain` matches; open other relevant fact bodies by name from the index.

Also read the plugin-shipped universal design principles and apply them at the code-writing stage (the web supplement is read only when web context is opted in — `GOODFELLOW_PRINCIPLES_WEB=1` or a `package.json` at the project root; an invalid value hard-errors here):

```bash
# One robust command: resolves + reads the seeded principles, with ALL error handling
# in Python (bad config / missing core / unreadable file -> non-zero exit + stderr).
# Its stdout IS the principles to apply (cite violations by P-NNN). A non-zero exit
# means a config/packaging problem — stop and surface it.
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/principles_context.py" --emit --project-root .
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
