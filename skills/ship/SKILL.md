---
name: ship
description: "Classify the risk tier, verify, prove new tests can fail, review the diff to convergence, open the PR, merge on autopilot, then extract learnings and file follow-up loops. --tier T0..T3 overrides the tier (never below its hard floor); --quick is an alias for --tier T0. Safety-critical findings block the PR."
---

Ship the current work: classify → verify → red check → review → PR → merge → learnings → loops.

**Dry-run (`GOODFELLOW_AUTOPILOT=dry-run`):** run the checks, but perform none of the writes below
(knowledge entries, loops, the PR, the merge). Log each as a `would_act` event to the run log
(`RUN_LOG=$(bash "${CLAUDE_PLUGIN_ROOT}/scripts/run_log.sh")`) instead.

Arguments: `[--tier T0..T3 | --quick] [--previous Tn]`. `--quick` means `--tier T0`. `--previous` is
the tier an earlier step of this run chose (brainstorm, or the `tier:` key of the plan or spec);
it can raise the tier, never lower it.

## 0. Ensure state directory

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/init_state.sh"
```

## 0.5 Classify the tier

Set `BASE` to the branch you will open the PR against (e.g. `origin/main`). Pick a tier from the
rubric in the `brainstorm` skill (T0 fix, T1 feature, T2 design, T3 live state; when unsure, the
higher one), then let the resolver apply the hard floors to the actual diff:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/tier.py" resolve --base "$BASE" --proposed <T0..T3> \
  [--tier <Tn> | --quick] [--previous <Tn>] [--live-state "<what live state it touches>"] \
  --reason "<one line: the rubric row that decided it>"
```

- **Exit 0:** announce its two lines (`Tier T0 (fix): …` / `Floor T0: …`) and continue at that tier.
- **Exit 3:** the requested `--tier`/`--quick` is below a hard floor (a high-stakes path forces
  T1, a live-state path or trigger forces T3). Say so with the resolver's reason and continue at the
  floor. Never honour it silently.
- **Exit 2:** no decision (bad base, a configured path list is missing). Stop; never assume T0.

**What the tier changes here:** the review's round cap (3 at T0 and T1, 6 at T2 and T3), the PR
body (a **Plan** section at T1, a **Rehearsal** section at T3), and nothing else. The checks below
run at every tier: verification, the red check, the mutation check when you keep a high-stakes
list, a review looped until no blocker or major remains, and the final-HEAD check.

**T3 gate.** At T3, before §2: the PR needs rehearsal evidence, meaning the real mutations were
performed against a sandbox (a database copy, a temporary tree, a namespace) and the result is
recorded. If there is none, HALT and say what rehearsal is missing. goodfellow does not provide the
sandbox; it refuses to skip the rehearsal.

## 1. Full verification pass

Run verification on the entire diff:

**Auto-detect toolchain** (same as execute):
- Python → ruff check + ruff format --check
- JS/TS → eslint or configured linter
- JSON → structural validation
- Tests → discover and run matching tests

If verification fails: surface errors, do not proceed to review.

### 1a. Tests that can fail

`BASE` is the branch from §0.5.

**Red evidence (P-094).** Every new test must fail on the base with an assertion, then pass:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/red_check.py" --base "$BASE"
```

- Exit 0: record the verdicts for the PR's Test evidence section. An `OK` proves the base failure was an assertion, not that it was the intended one: compare each printed base message with the expected red the plan named. If the branch changed an existing test's expectation (listed as not replayed), rerun with `--all-tests` and state in the PR why the old expectation was wrong. `NEW_SYMBOL` means the test calls code the base does not have, so a replay cannot show its red; cite the stub-first assertion red from execute instead.
- Exit 1: each `WRONG_REASON` / `NOT_RED` / `NOT_GREEN` / `SKIPPED` / `UNCLEAR` verdict is a review finding: major, or blocker on a high-stakes path. Fix the test (stub a wrong answer first, or make it assert the changed behaviour).
- Exit 2: the check did not run. Report it as not run, never as passed. For runners other than pytest, pass `--test-cmd` with `{tests}` and `{junit}` placeholders; if their JUnit failures name no assertion type (`UNCLEAR`), add `--assertion-type` or `--assertion-pattern`.

**Mutation check on high-stakes paths (P-095, optional).** Runs only when you keep a high-stakes path list (`.goodfellow/high_stakes_paths.txt`, one glob per line; see `configs/high_stakes_paths.example.txt`). It mutates only the Python lines this branch changed in those files, in throwaway copies, and reports every mutant the tests miss:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mutation_check.py" --base "$BASE"
```

- Exit 0: every mutant killed, or nothing in scope, or no path list (it says `SKIPPED`).
- Exit 1: each surviving mutant is a major finding. Kill it with a test before opening the PR, or write in the PR why it is equivalent (the mutated code behaves identically). Survivors neither killed nor explained are filed as loops per §5.
- Exit 2: red baseline, bad base, or a configured path list that does not exist, so the check did not run. It also refuses (exit 2) a target that sends signals, spawns processes, deletes or writes files. **Never mutation-test code that signals, deletes or writes real resources outside a fake or an isolated namespace**: a mutant can aim those calls at the wrong target, for example every process on the machine instead of one child. For signal/spawn code, `--isolated` runs the whole check inside its own PID namespace (`unshare --pid --fork --mount-proc`; it fails closed if none can be made). For delete/write code, and calls the check cannot see through (`getattr` on `os`/`shutil`, dynamic imports, `ctypes`), a PID namespace does not help: pass `--fakes` only when the tests replace those calls with fakes or temp directories. Otherwise leave the file out of the mutation run and rely on the deliberate-break evidence. Exit 3: the time budget ran out, so the result is incomplete. Neither is a pass (P-079).

## 2. Review

### The review loop (every tier)
Multi-round adversarial review on the diff. Same convergence algorithm as review-doc:
- Two reviewers per round (Claude + Codex/single-Claude fallback via bridge)
- Verifier pass at round 2+ (via `convergence_detector.py`)
- Research injection between rounds 1 and 2 if factual claims in findings
- Convergence when severity drops to polish-tier
- Hard cap: 3 rounds at T0 and T1, 6 at T2 and T3 (a cost bound; convergence, not the cap, ends a
  healthy loop)

```bash
# Review exactly what the PR will contain: commit the work first (stage files by name,
# including new ones), then review the branch against BASE.
[ -z "$(git status --porcelain)" ] || { echo "uncommitted changes: commit them before review" >&2; exit 1; }
git diff --quiet "$BASE"...HEAD && { echo "nothing to ship against $BASE" >&2; exit 1; }
OUT=$(bash "${CLAUDE_PLUGIN_ROOT}/scripts/codex-bridge.sh" --kind diff --base "$BASE") || {
  echo "review bridge failed: $OUT" >&2; exit 1; }
case "$OUT" in REVIEW_FAILED\ *) echo "review bridge failed: $OUT" >&2; exit 1 ;; esac
# On success $OUT is the review-artifact path; the Codex path is judged (see its
# `## Judge audit` section). Reject the REVIEW_FAILED sentinel before reading —
# never treat a failed review as an empty (zero-findings) pass.
```

A review whose artifact shows an empty diff while the branch differs from `BASE` is a failed
review, not a clean one.

**Failed-review contract:** if the bridge exits nonzero it prints `REVIEW_FAILED <code> <class>` instead of an artifact path. Treat that as a FAILED review, never clean/LGTM — reject the `REVIEW_FAILED` prefix before any read, surface it, and stop (do NOT proceed to PR/merge). A failed review is not a passed one.

### Final-HEAD check

The loop ends by *fixing* the last round's findings, so the last fix commit has been reviewed by
nobody. Before the PR is opened or merged, if HEAD moved after the last reviewed state, run one
narrow review of just the fix commits:

```bash
OUT=$(bash "${CLAUDE_PLUGIN_ROOT}/scripts/codex-bridge.sh" --kind diff --base <last-reviewed-sha>) || {
  echo "review bridge failed: $OUT" >&2; exit 1; }
case "$OUT" in REVIEW_FAILED\ *) echo "review bridge failed: $OUT" >&2; exit 1 ;; esac
```

It asks one question: did the fix introduce a defect? Only a blocker or major inside those commits
halts; anything else is recorded, not fixed here. It is a gate, not a round, and runs at most twice.

## 3. Ship-blocking check

**If any unresolved safety-critical finding remains at convergence or hard cap: HALT.** No PR creation, no merge. The finding must be fixed or the operator must explicitly waive it.

Filing as a loop is NOT sufficient for safety-critical findings at ship time.

## 4. Extract learnings to knowledge file

After the final review pass, scan the diff and review findings for new knowledge:

- **Principles:** design rules that emerged ("always validate at boundary X")
- **Patterns:** solutions that worked ("convergence-based termination")
- **Gotchas:** footguns discovered ("API returns null not undefined on empty")

Resolve the backend mode first (invalid `GOODFELLOW_MEMORY` hard-errors here):

```bash
MODE=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory_config.py" resolve-mode) || { echo "$MODE"; exit 1; }
```

**flat mode (`MODE=flat`, default — behavior unchanged):** Append candidates to `.goodfellow/knowledge.md` with `[pending]` tag and date:
```
- [pending] 2026-06-02: <learning text>
```

**rich mode (`MODE=rich`):** skip restatements of shipped principles (cite `P-NNN`), then write each kept candidate as a per-fact file:
```bash
# Fail CLOSED: a dedup error (drift / unparseable principles) must STOP, not silently
# persist a restatement. principles.md is required; the web supplement is optional.
DEDUP_FILES=( "${CLAUDE_PLUGIN_ROOT}/knowledge/principles.md" )
[ -f "${CLAUDE_PLUGIN_ROOT}/knowledge/principles-web.md" ] && DEDUP_FILES+=( "${CLAUDE_PLUGIN_ROOT}/knowledge/principles-web.md" )
PID=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/dedup_principles.py" --description "<learning text>" \
        --principles "${DEDUP_FILES[@]}") || exit 1
# if $PID non-empty: skip, log "skipped (restates $PID)"; else:
# Valid as written — substitute your own values. --name is a kebab-slug matching
# [a-z0-9-]; --type is one of principle|pattern|gotcha; --domain is optional (omit if none):
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory_index.py" --root .goodfellow write-fact \
  --name validate-at-boundary --description "Always validate at the boundary" \
  --type principle --status pending --opened "$(date +%F)" --body "Detail of the learning."
```

## 5. File follow-up loops

Every deferred finding from the convergence exit needs a durable destination — no
substantive tier may be silently dropped. Route by the three-tier severity taxonomy the
reviewers emit (`## Blockers` / `## Major` / `## Minor`; ranks blocker > major > minor,
per `SEVERITY_RANKS` in `convergence_detector.py`):

- **Safety-critical / blocker (rank 3)** → file to `.goodfellow/loops.json` via loop store. Blocker findings ALSO halt the PR per §3 — the loop is filed in addition to the halt, never as a substitute for fixing it.
- **Major (rank 2)** → file to `.goodfellow/loops.json` via loop store, the same destination as blocker. A major finding is substantive follow-up work, not a polish gotcha, so it belongs in the loop store. It does NOT block the PR (only blocker/safety-critical HALTs per §3), but it MUST be durably filed as a loop — never dropped, never downgraded to a knowledge gotcha.
- **Polish-tier / minor (rank 1)** → append to the knowledge file as gotchas instead of filing loops.

Summary: **blocker + major → loop store; minor → knowledge gotchas.** No severity tier is
left without a home. Loop priority comes from finding severity; round 4+ findings default
to p4 unless safety-critical.

Canonical routing map (severity → durable destination — the machine-readable source of
truth; keep the prose above and the README in sync with it, and never desync a tier to a
different destination):

```text
blocker -> loop_store        # substantive follow-up; ALSO halts the PR per §3
major   -> loop_store        # substantive follow-up; non-blocking, but MUST be filed — never a gotcha
minor   -> knowledge_gotchas # polish-tier
```

Pass `--lens <lens>` when the finding came from the judged Codex review: read the finding's `lens` cell from the review artifact's `## Judge audit` table (one of `auth-trust`, `data-integrity`, `failure-handling`, `concurrency`, `input-edge`, `compat-migration`, `observability`, `contract-scope`, or `other`). This threads the interpretation frame onto the loop so `triage` and `lens_tuning.py` can attribute outcomes per-lens. Omit `--lens` for hand-filed loops or an unjudged (fallback) review — a missing lens is fine (bucketed `unattributed`).

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/loop_store.py" --root . add "<title>" --priority <p> --source "ship-review-r<N>" --description "<text>" [--lens <lens>]
```

Soft cap check: if >15 active loops, warn "loop backlog growing — consider /goodfellow:triage".

## 6. Create PR

**Public / not-solely-owned target?** If this PR targets a repo you do not solely
control (a fork → upstream, an OSS contribution, any public repo), run the
`public-pr` gate FIRST — it scrubs the diff against your internal-ref denylist and
gives the correct cross-fork `gh pr create` mechanics. Skip it for your own repo.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/public_pr_scrub.py" --base "$BASE" || {
  echo "internal-ref scrub failed — do not open the public PR" >&2; exit 1; }
```

Create the PR with convergence and verifier stats in the description. State the review
exit honestly (P-079 — reaching a limit is not success): if round N was the hard cap with
findings still deferred, write "Halted at hard cap (round N)", not "Converged at round N".

```
## Summary
<what changed>

Tier: T1 (feature): <the resolver's reason>. Floor: <floor and why>.

## Plan (T1 only)
Goal / Approach / Tests, as written before building.

## Rehearsal (T3 only)
<the real mutations, the sandbox they ran against, and the result>

## Test evidence
- Red: <each new test and the assertion it failed with on the base> (red_check: N OK)
- Deliberate breaks: <break -> test that caught it>
- Mutation (high-stakes paths): K/M killed; <each survivor: killed by <test> or equivalent because <reason>> (or: not configured)

## Review stats
- Converged at round N, M findings resolved, K knowledge entries referenced
  (or: Halted at hard cap (round N) with D deferred findings — limit reached, not full resolution)
- Verifier: X findings verified, Y filtered (A stale, B noise)
- Knowledge: C new entries added ([pending])
- Loops: D follow-ups filed
```

## 7. Merge

**Autopilot (default):** merge when the review converged with no unresolved blocker, the final-HEAD
check is clean and CI is green. The stop list still applies: opening a PR on a public repository,
pushing to its default branch, a repository outside your owner list, releases and migrations stop
for the operator, whatever the tier. **Dry-run:** log `would_act: merge` instead.
**`GOODFELLOW_AUTOPILOT=0`:** ask once whether to merge.
