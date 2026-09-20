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

**Default: serial.** Implement every task inline yourself, in plan order. This is the baseline behavior and the safe default — do not deviate from it unless the fan-out decision in 2.0 justifies it.

### 2.0. Optional: parallel implementers, worktree-isolated (opt-in, per phase)

This is a capability the skill uses when a phase has **enough independent work to be worth fanning out** — it is not a default change to how plans execute. Most phases run serial. Parallel fan-out is the exception you justify from the plan's dependency graph *and* from the fan-out sizing rules below, not a mode you turn on by preference.

Each parallel implementer runs in its **own git worktree** off the current execution HEAD, and results are reconciled by **merge**, not by writing a shared tree. That isolation is what makes fan-out safe here: two children can no longer corrupt each other, because they never write the same working tree. A wrong independence call now costs a **merge conflict you resolve serially**, not silent mutual corruption.

**Step 1 — decide the fan-out count (MANDATORY: state it and why before dispatching).**

Before you create any worktree or dispatch any child, compute and **state out loud** the fan-out count you chose and the reason (e.g. "Phase 2 has 5 file-disjoint tasks; nproc=8 → cap 6; fanning out 5"). Do not dispatch without this line. Sizing rules (centrally set — identical to the son-of-anton coordinator philosophy so the two stay consistent):

- **FLOOR — below ~3 independent tasks, run serial.** For 1-2 items the coordination overhead plus the coordinator context spent creating worktrees, dispatching, and merging exceeds the wall-clock saved. (Anthropic scaling guidance: 1 subagent for a simple task, 2-4 for comparisons, 10+ only for genuinely complex fan-out.)
- **CEILING — tied to available concurrency ≈ `nproc - 2`.** The Agent-tool concurrency cap is `min(16, nproc - 2)`; dispatch no more children than that. Past the cap children just queue — no throughput gain, only straggler wall-clock and wasted coordinator context. Detect it with `getconf _NPROCESSORS_ONLN` (or `nproc`) and clamp: `fanout = max(1, min(candidate_count, 16, nproc - 2))`.
- If the count lands below the FLOOR after clamping, run the phase serial.

**Step 2 — pick the candidate set (file-disjointness is now an optimization hint, not a safety gate).**

From the not-yet-done tasks in the current phase, use the plan's dependency graph (the `what blocks what, what parallelizes` section) and declared target files to pick the fan-out set:

- Tasks with **no declared dependency edge** between them are candidates to run in parallel — a task that depends on another's output must still run after it.
- **File-disjointness is a hint that predicts clean merges, not a correctness precondition.** Prefer file-disjoint tasks because they merge without conflict. Tasks that share a file are *allowed* to fan out under isolation, but they will likely need a serial conflict resolution at merge time — factor that cost in, and when in doubt about the payoff, keep overlapping tasks serial. What you are no longer doing is treating overlap as a corruption hazard; isolation removed that.

**Step 3 — dispatch one worktree-isolated implementer per task.**

For each task in the fan-out set, create a dedicated worktree off the current execution HEAD and dispatch one implementer subagent (vanilla Agent/Task tool) into it:

```bash
# BASE = current execution branch HEAD; SLUG = task id, e.g. t-2-3
git worktree add -b "gf-exec/<SLUG>" "../gf-exec-<SLUG>" HEAD
```

Instruct each child to work **only inside its worktree path**, run the per-task loop below (2a-2e) there, and **commit its result on its own branch** before returning. Children touch only their own worktree — never the parent checkout, never another child's.

**Liveness (relaxed, because isolation removed the corruption risk).** goodfellow still has no parent-side liveness watchdog, but a hung or reaped child can no longer damage the shared tree — its work is quarantined in its own worktree and branch. So the old hard "foreground, short-lived only" constraint is relaxed to a bound, not a prohibition:
- Keep child tasks **well-scoped and bounded** so a straggler doesn't stall the phase.
- If a child hangs or fails to return within a reasonable bound, **abandon its worktree and run that one task serially in the parent** — the other children's committed branches are unaffected. Discard the dead worktree with `git worktree remove --force`.
- Because a dead child costs only its own task (not the phase), children **may** run concurrently rather than being forced strictly foreground one-at-a-time.

**Step 4 — reconcile by merge, then verify together.**

Once children return, merge each child branch back into the execution branch in plan order:

```bash
git merge --no-ff "gf-exec/<SLUG>"   # repeat per child, in dependency order
```

- A **clean merge** confirms the tasks were disjoint as predicted — continue.
- A **merge conflict** means the disjointness hint was wrong for those files. This is a conflict, not corruption: resolve it serially, keeping both children's intent, then continue.
- After all merges, run the per-task verify (2d) across **all** affected files together to catch cross-task integration breakage.
- Clean up worktrees when done: `git worktree remove "../gf-exec-<SLUG>"` for each (`--force` if a child left it dirty), and delete merged branches.

For each task run serially (or each task within a fanned-out set, executed by the child implementer in its worktree): follow the loop below, in plan order.

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
