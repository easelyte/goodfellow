### 2.0. Optional: parallel implementers, worktree-isolated (opt-in, per phase)

This is a capability the skill uses when a phase has **enough independent work to be worth fanning out** — it is not a default change to how plans execute. Most phases run serial. Parallel fan-out is the exception you justify from the plan's dependency graph *and* from the fan-out sizing rules below, not a mode you turn on by preference.

Each parallel implementer runs in its **own git worktree** off a checkpoint of the current execution state, and results are reconciled by **merge**, not by writing a shared tree. That isolation is what makes fan-out safe here: two children can no longer corrupt each other, because they never write the same working tree. But isolation only holds if it is *enforced at the runtime layer* (Step 4) — a prose instruction to "stay in your worktree" is not isolation. A wrong independence call then costs a **merge conflict you resolve serially**, not silent mutual corruption.

**Step 1 — decide the fan-out count (MANDATORY: state it and why before dispatching).**

Before you checkpoint, create any worktree, or dispatch any child, compute and **state out loud** the fan-out count you chose and the reason (e.g. "Phase 2 has 5 independent tasks; runtime concurrency cap ~8; fanning out 5"). Do not dispatch without this line. Sizing rules (a deliberate policy, not per-run improvisation — apply them consistently):

- **FLOOR — below ~3 independent tasks, run serial.** For 1-2 items the coordination overhead plus the coordinator context spent checkpointing, creating worktrees, dispatching, and merging exceeds the wall-clock saved. (Anthropic scaling guidance: 1 subagent for a simple task, 2-4 for comparisons, 10+ only for genuinely complex fan-out.)
- **CEILING — never dispatch more children than the runtime can actually run concurrently.** Past the real cap children just queue — no throughput gain, only straggler wall-clock and wasted coordinator context. The true cap is set by the harness, not by CPU count, and is often lower than you'd guess (a common default is ~10 concurrent tool uses, and some hosts configure it lower). Do not hardcode a formula as if it were authoritative: read the runtime's concurrency setting if it exposes one, otherwise treat capacity as unknown and use a conservative bound (a `nproc - 2` estimate, capped at ~8-10, is a reasonable fallback — not a guarantee). Clamp the count to that bound.
- If the count lands below the FLOOR after clamping, run the phase serial.

**Step 2 — pick the candidate set (file-disjointness is now an optimization hint, not a safety gate).**

From the not-yet-done tasks in the current phase, use the plan's dependency graph (the `what blocks what, what parallelizes` section) and declared target files to pick the fan-out set:

- Tasks with **no declared dependency edge** between them are candidates to run in parallel — a task that depends on another's output must still run after it.
- **File-disjointness is a hint that predicts clean *textual* merges, not a correctness precondition and not proof of semantic independence.** Prefer file-disjoint tasks because they merge without textual conflict. Tasks that share a file are *allowed* to fan out under isolation, but they will likely need a serial conflict resolution at merge time — factor that cost in, and when in doubt about the payoff, keep overlapping tasks serial. What you are no longer doing is treating overlap as a corruption hazard; isolation removed that. Note the limit: even file-disjoint tasks can be *semantically* coupled (a rename, shared invariant, schema, or ordering assumption) — the plan's dependency graph, not disjointness, is what rules that out, and Step 6's integration verify is the backstop.

**Step 3 — checkpoint the full execution state before fanning out.**

Children branch from a commit, so anything not committed is invisible to them. Before creating any worktree, make the current execution state recoverable and complete — **without sweeping in unrelated changes**:

- **Audit the index first, and checkpoint an explicit pathspec.** The §0 workspace check is advisory, so the tree may be dirty with changes you do not own. Never checkpoint with a blanket `git add -A` or a bare `git commit`: derive the explicit pathset of files this phase's completed tasks own and commit **only that pathspec** (`git add <task-files>` then commit). If unrelated paths are already staged, abort and surface them rather than absorbing them — a checkpoint that captures the operator's unrelated staged work propagates it to every child and into the final PR.
- **Commit completed-but-uncommitted task work** from earlier in this phase/run onto the execution branch (the serial loop marks tasks done without committing) — otherwise children start from stale code missing their prerequisites.
- **Make the plan artifact and any untracked prerequisite inputs available to children.** The plan file is usually still untracked at execution time; commit it (or otherwise place it inside each worktree) so children can read their own task bodies and acceptance criteria.
- Record this **checkpoint commit SHA** — every child must start from exactly it (Step 4), not from a bare `HEAD` that predates the phase's work nor a fresh base that omits it.

**Step 4 — dispatch one worktree-isolated implementer per task.**

Dispatch one implementer subagent (vanilla Agent/Task tool) per task. Two invariants MUST hold for every child, and neither is satisfied by a prose instruction to the child:

1. **Isolation is enforced by the runtime, not by asking.** Subagents start in the *parent's* working directory and `cd` does not persist between their tool calls, so a child using ordinary relative Edit/Write/Bash mutates the parent checkout and recreates the shared-tree corruption this design exists to prevent. Only a runtime that reroots and confines the child's working root actually prevents this — absolute-path and pre-write instructions are best-effort prose, not enforcement.
2. **The child starts from the checkpoint SHA (Step 3), not a fresh/default base.** Claude Code's runtime worktree isolation branches from a base governed by a `worktree.baseRef` setting whose **default (`fresh`) is `origin/<default-branch>` — NOT your local HEAD/checkpoint.** Left unpinned, children silently run against stale code missing the phase's work and the just-committed plan.

**Preferred — runtime-enforced isolation.** Use the Agent tool's built-in `isolation: "worktree"` (or a harness equivalent that actually reroots the child), the only mechanism that *enforces* invariant 1. Configure it to also satisfy invariant 2: (i) pin its base to the checkpoint SHA (e.g. set `worktree.baseRef=head` with the checkpoint on HEAD, or pass an explicit base), (ii) have each child **verify its starting `HEAD == <CKPT>` before any write and fail closed to serial on mismatch**, and (iii) **capture the runtime-created branch/commit ref** it returns — do not assume a name — for the Step-6 merge.

**Degraded fallback — manual worktree, NOT runtime-enforced.** Only if no runtime isolation option exists, create the worktree yourself off the checkpoint:

```bash
# CKPT = the Step-3 checkpoint SHA; SLUG = task id, e.g. t-2-3
git worktree add -b "gf-exec/<SLUG>" "$(pwd)/../gf-exec-<SLUG>" "<CKPT>"
```

Understand this does **not** confine the child at the runtime layer — a child that ignores its instructions can still write the parent checkout. Mitigate with (a) the child's **absolute** worktree path, (b) **absolute paths for every file operation** (never relative), and (c) a **mandatory pre-write assertion**: `git rev-parse --show-toplevel` must equal the allocated path or the child refuses to write. **If you cannot guarantee runtime-enforced confinement and do not fully trust these prose controls, run the phase serial instead** — serial is strictly safer than an unenforced "isolated" fan-out.

Each child runs the per-task loop in `tasks.md` in this skill's directory (2a–2e) inside its worktree and **commits its result on its own branch** before returning; record that branch/commit ref for reconciliation.

**Step 5 — straggler / liveness handling (never force-remove a worktree whose child may be live).**

goodfellow has no parent-side liveness watchdog and cannot portably hard-kill a hung subagent (see `../grill/interview.md` relative to this skill's directory). Isolation removed the *corruption* risk — a slow or dead child can no longer damage the shared tree — but it did NOT make forced cleanup safe, because you cannot prove a hung child has stopped writing:

- Keep child tasks **well-scoped and bounded** so a straggler doesn't stall the phase. Because a dead child costs only its own task (not the phase), children **may** run concurrently rather than strictly foreground one-at-a-time.
- If a child exceeds a reasonable bound: **do not `git worktree remove --force` a worktree whose child you cannot confirm has terminated** — that can delete uncommitted work and yank the directory out from under a still-running writer. Instead **quarantine** it (leave it in place, do not reuse it). Whether you may then *replay* the task depends on its effects: a **filesystem-only** task can be re-run serially in a fresh worktree (the quarantined child touches only its own tree). A task with **external, non-idempotent effects** (DB migration, API call, deploy, notification, package publish) must **NOT** be concurrently replayed — a worktree isolates files, not a shared database or remote, so replaying while the first execution is still live duplicates the mutation. For those, **halt to the operator** unless the task carries a stable idempotency key and you can check whether the effect already happened. The other children's committed branches are unaffected either way.
- Only remove a worktree after the child is **confirmed finished** AND its working tree is either clean or its dirty diff has been inspected and preserved (or proven redundant). Reserve `--force` for that confirmed-dead, already-captured case.

**Step 6 — reconcile by merge, then verify together.**

Once children return, merge each child branch back into the execution branch in plan order:

```bash
git merge --no-ff "<child-branch-ref>"   # the ref captured in Step 4; repeat per child, in dependency order
```

- A **clean merge means only that the edits did not textually conflict** — it does NOT prove the tasks were semantically independent. Two cleanly-merging children can still disagree about a renamed interface, an invariant, a schema, or an ordering assumption.
- A **merge conflict** means the changes overlapped textually. This is a conflict, not corruption: resolve it serially, keeping both children's intent, then continue.
- After all merges, run the per-task verify (2d) across **all** affected files together, **and re-check the merged diff against every affected task's acceptance criteria** — this integration pass, not the merge result, is what catches semantic incompatibility a clean merge hides.
- Clean up only **confirmed-finished** worktrees (per Step 5): `git worktree remove` each (`--force` only once the child is proven done and its work captured), and delete merged branches.

For each task run serially (or each task within a fanned-out set, executed by the child implementer in its worktree): follow the loop in `tasks.md` in this skill's directory, in plan order.

