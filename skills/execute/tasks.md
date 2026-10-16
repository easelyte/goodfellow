## 2. Per-task implementation loop

**Default: serial.** Implement every task inline yourself, in plan order. This is the baseline behavior and the safe default — do not deviate from it unless the fan-out decision in §2.0 of `parallel.md` in this skill's directory justifies it.

### 2a. Read the task
Read the task body, acceptance criteria, and dependencies. Check that dependencies are complete.

### 2b. Check gotchas
Scan the task's target files/modules against knowledge gotchas. If a gotcha matches, surface it before implementing: "Knowledge gotcha: <entry>. Accounting for this in implementation."

### 2c. Implement test-first
Write the code/config/docs the task specifies. Follow acceptance criteria. For any task that adds or changes behaviour:

1. **Red for the right reason (P-094).** Write the task's tests first, run them, and read the failure. It must be an assertion failure that names the expected behaviour. An ImportError, NameError, AttributeError or collection error does not count: add a stub that returns a wrong answer, rerun, and get the assertion red.
2. **Green.** Implement until the tests pass. **Never edit an expected value to match the output.** Change an expectation only when you can state why the old one was wrong, and say so in the task report.
3. **Deliberate break (P-095), high-stakes tasks.** For each break the plan names (at least one per rule): save a copy (`cp <file> <file>.orig`), apply the break, run the tests, confirm one goes red, then restore with `mv <file>.orig <file>`. Never restore with `git checkout -- <file>`, `git restore` or `git stash`: those also discard your uncommitted real edits. If the code sends signals, deletes, writes outside a temp directory or spawns processes, run the broken copy only against fakes or in an isolated namespace: a break that flips "is this ours?" turns cleanup code against the whole machine.
4. **Record the evidence** for the task: each new test's red message, and each break with the test that caught it. ship copies it into the PR.

### 2d. Verify
After implementation, run verification:

**Auto-detect toolchain:**
- Python files changed → `ruff check` + `ruff format --check` (if ruff installed)
- JS/TS files changed → `eslint` or project's configured linter (if installed)
- JSON files changed → `python3 -c "import json; json.load(open('<file>'))"` structural validation
- Test files matching changed modules → discover and run them

If verification fails: fix the issue before proceeding. Do not silently continue.

**Optional mechanical red check.** Once the task's tests exist, replay them against the base to confirm each new test fails there on an assertion and passes here (runner-agnostic via JUnit; `--test-cmd` keeps the sandbox; only `--command` or trusted base config `red_check.command` skips bubblewrap, announces that choice and retains credentials in the environment):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/red_check.py" --base <base-branch>
# 0 = no bad verdict; 1 = WRONG_REASON / NOT_RED / NOT_GREEN / SKIPPED / UNCLEAR: fix the test;
# 2 = the check itself failed (unknown base, runner wrote no report): not a pass.
```

### 2e. Mark complete
Note the task as done. Proceed to next task.

**Autopilot (default):** proceed through all tasks without pausing and report progress at phase
boundaries. With `GOODFELLOW_AUTOPILOT=0`, pause at each phase boundary for the operator's go.

**T3 live steps are never executed here.** A T3 plan's `## Live step` section is not a task: do not
run it, whatever the autopilot setting. `ship` hands it to the operator after review and CI. If a
task would itself mutate live state (production data, a deploy, a real send), stop and ask.

