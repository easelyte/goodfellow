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

**Default: serial.** Implement every task inline yourself, in plan order. This is the baseline behavior and the safe default — do not deviate from it unless the gate in 2.0 provably clears.

### 2.0. Optional: gated parallel implementers (opt-in, per phase)

This is a capability the skill uses **only when the plan makes independence provable** — it is not a default change to how plans execute. Most phases run serial. Parallel fan-out is the exception you must justify from the plan's own dependency graph, not a mode you turn on by preference.

**When it may apply:** at a phase boundary, look at the set of not-yet-done tasks in the current phase and the plan's stated dependency graph (the `what blocks what, what parallelizes` section). If a subset of those tasks is provably **file-disjoint** — no two of them touch the same file, per the plan's declared target files and dependency edges — you MAY fan out one implementer subagent per independent task (vanilla Agent/Task tool, following `superpowers:dispatching-parallel-agents`), then reconcile their results before continuing.

**MANDATORY independence gate (load-bearing — do not soften):**
- Parallelize a set of tasks ONLY if the plan's stated dependency graph proves they are file-disjoint: no shared target file, no declared dependency edge between them.
- On **ANY** file overlap, ambiguity, or uncertainty about what a task touches → **fall back to serial** for those tasks. Do not guess. Do not parallelize "probably independent" tasks.
- The default is serial; parallel is the exception. If you cannot cite the specific dependency-graph facts that prove disjointness, you have not cleared the gate — run serial.

**Portability caveats (state these explicitly; they are why the gate is strict):**
- goodfellow has **NO git-worktree isolation** and **NO #39-style parent-side liveness watchdog**. There is no worktree safety net.
- (a) Parallel children write a **shared working tree**. If two children touch the same file they WILL collide and corrupt each other's work — the disjointness gate is the *only* thing preventing this. Nothing else will catch it.
- (b) A hung child is **unrecoverable** — there is no TaskStop watchdog to reap it. Keep parallel children **foreground, short-lived, and bounded** (small, well-scoped tasks; never long-running or open-ended). Do not background them.
- (c) If in doubt, serial.

**Reconcile after fan-out:** once the parallel children return, integrate their changes, then run the per-task verify (2d) across all affected files together, and resolve any conflict serially. Then continue to the next phase.

For each task run serially (or each task within a fanned-out set, executed by the child implementer): follow the loop below, in plan order.

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
OUT=$(bash "${CLAUDE_PLUGIN_ROOT}/scripts/codex-bridge.sh" --kind diff --uncommitted) || {
  echo "review bridge failed: $OUT" >&2; exit 1; }
case "$OUT" in REVIEW_FAILED\ *) echo "review bridge failed: $OUT" >&2; exit 1 ;; esac
# On success $OUT is the review-artifact path (Codex path is judged; see its
# `## Judge audit` section). Reject the REVIEW_FAILED sentinel before reading.
```

**Failed-review contract:** if the bridge exits nonzero it prints `REVIEW_FAILED <code> <class>` instead of an artifact path. Treat that as a FAILED review, never clean/LGTM — reject the `REVIEW_FAILED` prefix before any read, surface it, and stop; do not treat a failed review as a clean phase boundary.

Surface any findings. Fix blockers before proceeding to next phase.

In interactive mode, pause briefly: "Phase N complete. M tasks done. Continuing to Phase N+1."

## 4. After all tasks

Summarize honestly (P-079 — reaching a limit is not success). Reaching the end of the task
list is not the same as completing the work, so the *whole* summary is conditional — do not
lead with a success claim you then walk back:

- **Success path** — every task completed AND all required verification ran and passed:
  "Execution complete. N tasks across M phases. Verification passed."
- **Partial/halted path** — any task stopped at a blocker or limit, or any verification was
  skipped, partial, or failing: "Execution halted after K of N tasks." Enumerate the
  remaining tasks and any unrun/failed verification (e.g. "Stopped at T-x.y: <reason>";
  "Verification skipped for T-x.y"). Never emit "Execution complete" on this path.

**Terminal gate before shipping (P-079).** Dispatch ship ONLY on the success path above. On
the partial/halted path, do NOT auto-dispatch ship — HALT and surface the incomplete state
to the operator. Under autopilot, stop the chain here rather than cascading; do not ship
partial or unverified work. Filing the gaps as follow-ups is not a substitute for the halt.

Otherwise (success path) auto-dispatch `/goodfellow:ship`.
