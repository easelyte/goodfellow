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
sandbox; it refuses to skip the rehearsal. The live step itself comes last (§7a).

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

Both checks below run your tests inside a sandbox where bubblewrap is installed (a private PID
namespace and a filesystem allowlist; see `docs/configuration.md`). Without it, the red check runs
unsandboxed and says so: note "unsandboxed" in the PR's Test evidence. The mutation check refuses
without it unless `--fakes` applies. When either check exits 2 over the sandbox (bubblewrap missing
for the mutation check, or installed but not isolating), report the check as not run, quote its
message, and never rerun it with `GOODFELLOW_SANDBOX=off` or `--fakes` on your own: only the user
decides that.

**Red evidence (P-094).** Every new test must fail on the base with an assertion, then pass:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/red_check.py" --base "$BASE"
```

- Exit 0: record the verdicts for the PR's Test evidence section. An `OK` proves the base failure was an assertion, not that it was the intended one: compare each printed base message with the expected red the plan named. If the branch changed an existing test's expectation (listed as not replayed), rerun with `--all-tests` and state in the PR why the old expectation was wrong. `NEW_SYMBOL` means the test calls code the base does not have, so a replay cannot show its red; cite the stub-first assertion red from execute instead.
- Exit 1: each `WRONG_REASON` / `NOT_RED` / `NOT_GREEN` / `SKIPPED` / `UNCLEAR` verdict is a review finding: major, or blocker on a high-stakes path. Fix the test (stub a wrong answer first, or make it assert the changed behaviour).
- Exit 2: the check did not run. Report it as not run, never as passed. For runners other than pytest, pass `--test-cmd` (keeps the sandbox) with `{tests}` and `{junit}` placeholders; if their JUnit failures name no assertion type (`UNCLEAR`), add `--assertion-type` or `--assertion-pattern`.

**Mutation check on high-stakes paths (P-095, optional).** Runs only when you keep a high-stakes path list (`.goodfellow/high_stakes_paths.txt`, one glob per line; see `configs/high_stakes_paths.example.txt`). It mutates only the Python lines this branch changed in those files, in throwaway copies, and reports every mutant the tests miss:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mutation_check.py" --base "$BASE"
```

- Exit 0: every mutant killed, or nothing in scope, or no path list (it says `SKIPPED`).
- Exit 1: each surviving mutant is a major finding. Kill it with a test before opening the PR, or write in the PR why it is equivalent (the mutated code behaves identically). Survivors neither killed nor explained are filed as loops per §5.
- Exit 2: red baseline, bad base, or a configured path list that does not exist, so the check did not run. It also refuses (exit 2) a target that sends signals, spawns processes, deletes or writes files. **Never mutation-test code that signals, deletes or writes real resources outside a fake or an isolated namespace**: a mutant can aim those calls at the wrong target, for example every process on the machine instead of one child. For signal/spawn code, pass `--isolated`: the sandbox already gives every test run its own PID namespace, and with `GOODFELLOW_SANDBOX=off` the flag runs the whole check inside one (`unshare --pid --fork --mount-proc`; it fails closed if none can be made). Neither isolates the network, so code that calls a live service needs fakes. For delete/write code, and calls the check cannot see through (`getattr` on `os`/`shutil`, dynamic imports, `ctypes`), a PID namespace does not help: pass `--fakes` only when the tests replace those calls with fakes or temp directories. Otherwise leave the file out of the mutation run and rely on the deliberate-break evidence. Exit 3: the result is incomplete: the time budget ran out, or a mutant timed out under a limit shorter than 3x the suite's runtime under load, which proves no hang. Neither is a pass (P-079).
- A verdict marked `(SAMPLED: file k/N)` covers a seeded sample of a large new file (over 400 lines, more than 150 mutants). Say so in the PR's Test evidence; edits to existing files are never sampled.


Only `--command` or trusted base config `red_check.command` selects a custom runner: it skips bubblewrap, prints a note, and keeps credentials in the environment. A reset alone keeps the default sandbox path.
