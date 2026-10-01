# Configuration

Everything in goodfellow works without configuration. This page is the full reference; the
[README](../README.md) covers the common settings.

## Environment variables

Everything works without configuration. These environment variables change the defaults; invalid
values fail loudly rather than falling back.

| Variable | Default | Purpose |
|---|---|---|
| `GOODFELLOW_AUTOPILOT` | on | Autopilot is the default. `0` pauses for your go at each step; the stop list stays on. `dry-run` logs decisions without changing project files. |
| `GOODFELLOW_STOP_LIST` | on | `0` is the dedicated opt-out: it turns the stop list off in every mode. The other built-in guards stay on. |
| `GOODFELLOW_CODEX` | `1` | `0` disables Codex even when it is installed. |
| `GOODFELLOW_CODEX_MODEL` | Codex default | GPT model for the Codex reviewer. |
| `GOODFELLOW_REVIEW_MODEL` | see below | Claude reviewer model: `opus`, `sonnet` or `haiku`. |
| `GOODFELLOW_CODEX_STAGE_TIMEOUT` | `300` | Seconds per Codex stage. Generator and judge are two stages. |
| `GOODFELLOW_TRUST_ANALYZERS` | unset | `1` also runs `eslint`, `tsc` and `mypy` in the review pre-pass. These execute project config, so only for repositories you trust. |
| `GOODFELLOW_TAVILY_KEY` | unset | Tavily API key for batch research. Without it, research uses Claude's web search. |
| `GOODFELLOW_MEMORY` | `flat` | Knowledge backend: `flat` or `rich` (see below). |
| `GOODFELLOW_MEMORY_WARN_KB` | `16` | Rich mode: warn when the index exceeds this size. |
| `GOODFELLOW_PRINCIPLES_WEB` | auto | `1` loads the web principles (JS, React, Next.js, Postgres). Auto-enabled when a `package.json` is present. |
| `GOODFELLOW_HIGH_STAKES_PATHS` | `.goodfellow/high_stakes_paths.txt` | Glob list that sets a T1 floor and enables the mutation check. |
| `GOODFELLOW_LIVE_STATE_PATHS` | `.goodfellow/live_state_paths.txt` | Globs added to the built-in T3 list; a `!glob` line drops a built-in one. |
| `GOODFELLOW_GUARDS` | `1` | `0` turns off the built-in guards and the stop list. Project rules still apply. |
| `GOODFELLOW_SANDBOX` | `bwrap` | The test sandbox for the red and mutation checks (see below). `off` runs their tests unisolated, knowingly. |
| `GOODFELLOW_SANDBOX_RO` | unset | Extra read-only paths inside the sandbox, separated by `:`. |
| `GOODFELLOW_BWRAP` | `bwrap` on `PATH` | Path to the bubblewrap binary. |
| `GOODFELLOW_TRIAGE_RETENTION_DAYS` | `90` | Days to keep closed triage entries. |
| `GOODFELLOW_RUNS_RETENTION_DAYS` | `90` | Days to keep autopilot run logs. |

## Reviewers and models

With the Codex CLI installed, the bridge reviewer is Codex (generator plus judge), and
`review-doc` adds a parallel Claude reviewer (default `sonnet`). Without Codex, the
bridge falls back to a single Claude reviewer (default `opus`, so the only reviewer is not weaker than
the model that wrote the code). Setting `GOODFELLOW_REVIEW_MODEL` overrides both Claude reviewers; it
never reaches the Codex path.

A practical setup is Opus for your main session with Codex and Sonnet reviewing. Codex is worth
having mainly for cost: an adversarial pass from a different model family on every change, at little
marginal cost on a Codex subscription.

For `--commit` and `--base` reviews the bridge can prepend a static-analysis digest from `ruff`,
`shellcheck`, `gitleaks` (with the vendored [`configs/gitleaks.toml`](../configs/gitleaks.toml)) and
`semgrep` (vendored local rules, never `--config auto`). Each is used if installed and skipped with
a note if not.

## Tool-layer guards

A rule whose violation is expensive to undo should not live only in a prompt: compaction can drop it,
and the session that inherits the summary was never told. Goodfellow's `PreToolUse` hook
(`scripts/guard_engine.py`) checks every tool call instead.

Built in, on by default:

- `git add -A`, `git add .`, `git add --all`: stage specific files instead.
- `--dangerously-skip-permissions`.
- Force-push to a protected branch (`main` and `master` by default). Feature branches are not affected.

Matching is by shell token, and the built-ins only inspect `Bash` commands, so documenting a flag in a
file or a commit message does not trip a guard.

Add project rules in `.goodfellow/guards.json` (see
[`configs/guards.example.json`](../configs/guards.example.json)):

```json
{
  "protected_branches": ["main", "master"],
  "stop_list": { "owners": ["your-user", "your-org"] },
  "block": [
    {
      "id": "no-prod-db-writes",
      "match": "regex",
      "pattern": "psql.*(prod|production)",
      "flags": "i",
      "reason": "Prod DB writes need a human.",
      "tools": ["Bash"],
      "bypass_env": "PROD_DB_OK"
    }
  ]
}
```

`python3 scripts/guard_engine.py --validate` checks a config (non-zero on error, for CI) and
`--selfcheck` prints what is enforced. A malformed config at runtime skips the project rules with a
warning but keeps the built-ins, so a typo cannot lock you out of fixing it. `CLAUDE_HOOK_BYPASS=1`
disables all guards for one command.

## Test sandbox

`red_check.py` and `mutation_check.py` run your test suite, and the mutation check runs it against
deliberately broken code: a mutant of a cleanup routine can delete the wrong directory, and a
mutant of process-selection code can signal the wrong process. So every test command they run goes
through [bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`):

- **A private PID namespace.** The tests see and can signal only their own processes.
- **A filesystem allowlist.** Read-only: `/usr` and the `/bin`, `/lib` links, a short list of
  `/etc` files, the Python interpreter and its site-packages, the directories on `PATH`, and
  anything in `GOODFELLOW_SANDBOX_RO`. `/tmp`, `/var/tmp`, `/run` and `HOME` are private and empty.
  The only writable host directory is the check's own throwaway copy. Your home directory, your
  checkout and your credentials are not mounted.

Before the first test runs, a probe goes through the same wrapper. It must show a private PID
namespace, no write reaching your home directory or your checkout, and none of the usual
credential paths (`~/.ssh`, `~/.aws`, `~/.config/gh`, ...). If `bwrap` is missing or the probe
fails, the check exits 2 and runs nothing. There is no silent fallback.

- **Linux:** install bubblewrap (`apt install bubblewrap`, `dnf install bubblewrap`,
  `pacman -S bubblewrap`). On Ubuntu 24.04 and later, unprivileged user namespaces may be
  restricted by AppArmor; use the distribution's `bwrap` package, which ships a profile, or allow
  them for your user.
- **macOS, or a container without user namespaces:** `GOODFELLOW_SANDBOX=off` runs the tests
  unisolated, knowingly. Every run warns, and the JSON report records `"sandbox": "off"`.
- **Tests that need files outside the allowlist** (a toolchain under `/opt`, fixtures elsewhere):
  add the paths to `GOODFELLOW_SANDBOX_RO`. The network is not isolated.

With the sandbox on, the mutation check accepts targets that signal or spawn processes without
`--isolated`, because every run already has its own PID namespace. Targets that delete or write
files still need `--fakes`.

## Knowledge and memory backends

**`flat` (default).** One append-only file, `.goodfellow/knowledge.md`, with three sections:

```markdown
## Principles
- 2026-06-02: Validate at the boundary, never trust upstream sanitization

## Patterns
- 2026-06-02: Stop reviewing when severity drops, not when the finding count hits zero

## Gotchas
- [pending] 2026-06-02: The Codex CLI has no --file flag; use --commit/--base/--uncommitted
```

`ship` and `snap-compact` add entries tagged `[pending]`; `close` confirms them.

**`rich` (`GOODFELLOW_MEMORY=rich`).** One file per fact under `.goodfellow/memory/`, a regenerated
index at `.goodfellow/MEMORY.md`, and per-domain registries. Writes are atomic, locked and journaled
(a two-phase write-ahead log with rollback), and the first rich write migrates an existing
`knowledge.md` without modifying it. Worth it when the flat file becomes unwieldy; most projects never
need it. Switching back to `flat` is safe.

## Follow-up loops and triage

At ship time, deferred review findings are routed by severity:

- **Blocker** (security, data loss, correctness): stops the pull request until fixed or explicitly
  waived.
- **Major:** filed as a loop in `.goodfellow/loops.json` (priority `p1` to `p4`).
- **Minor:** added to the knowledge file as a gotcha.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/loop_store.py" list   # open loops
/goodfellow:brainstorm --from-loop 3                          # design a fix for loop 3
/goodfellow:triage                                            # sort real defects from noise
```

`triage` has two reviewers assess each loop independently, reconciles their verdicts, and asks you
to confirm in one table. Decisions go to `.goodfellow/triage-log.jsonl`. A loop can stay "unclear"
for at most three cycles. Findings from review round four onwards are filed at the lowest priority,
and a warning appears at 15 open loops.

## Autopilot and the stop list

Autopilot is on by default: the chain runs without approvals between steps. It stops only for
product calls (naming, pricing, public positioning, taste-only UX, scope beyond the request) and for
the stop list, which a `PreToolUse` hook enforces (`scripts/stop_list.py`):

| Stop | What it catches |
|---|---|
| `stop-foreign-remote` | `git push` or `gh pr create` to a repository whose owner is not in `stop_list.owners` (entries `owner` for GitHub, or `host/owner`; default: the host and owner of `origin`), or whose destination cannot be resolved. The same account name on another host counts as foreign. |
| `stop-public-repo` | On a public repository you own: a push to its default branch, a tag push, or `gh pr create`. Visibility is looked up with `gh repo view` and cached for ten minutes; a failed lookup stops the action. The push target honours `git -c` overrides, `remote.<name>.push`, `push.default` and every URL of the remote. Pushes to other branches are checked against the live default branch too, but a failed lookup never blocks them. |
| `stop-release` | `gh release create/upload/edit/delete` and `gh api` writes to a releases endpoint. |
| `stop-publish` | `npm publish`, `twine upload`, `cargo publish`, `docker push` and similar. `--dry-run` is allowed. |
| `stop-migration` | Deploy-style migrations: `prisma migrate deploy`, `alembic upgrade`, `manage.py migrate`, `rails db:migrate` and similar. Replace the list with `stop_list.migration_commands`. |
| `stop-force-push` | `git push --force`, `-f` or a `+refspec`. `--force-with-lease` to a feature branch is allowed. |

Sending messages, spending money and product calls cannot be read from a command line; the skills
carry those as written rules. The stop list is on in every mode: `GOODFELLOW_AUTOPILOT=0` only
brings back step approvals. `GOODFELLOW_STOP_LIST=0` turns the stop list off (its dedicated
opt-out), and `GOODFELLOW_GUARDS=0` turns off every built-in guard including it. `dry-run` shows what it would do and writes only the decision log
(`.goodfellow/runs/<timestamp>-<pid>.jsonl`). Turn off one stop with its id in `disable_builtins`.

## Principles

Goodfellow is built on a few convictions:

- **A second model family finds what the first rationalised away.** Same-model review mostly repeats
  the author's blind spots.
- **Ground findings in facts.** Claims are researched before review, and findings are checked
  against the code before anyone fixes them.
- **Stop on severity, not on a round count.** And reaching a limit is not success: a cap, budget or
  timeout is reported as a halt, never as done.
- **A test is evidence only if it could have failed.** Red before green, for the right reason
  (P-094). Break the code on purpose to prove a test notices (P-095). Test behaviour through the real
  entry point, not the source text (P-096).
- **Constraints that matter belong in the tool layer**, not only in a prompt that compaction can
  drop.
- **Follow-ups need an owner.** A deferred finding is tracked and triaged, not noted and forgotten.
- **Knowledge should compound.** Every run leaves the next one better informed.

These convictions, and many more specific ones, ship as about 80 seeded principles in
[`knowledge/principles.md`](../knowledge/principles.md) (stack-agnostic) and
[`knowledge/principles-web.md`](../knowledge/principles-web.md) (JS, React, Next.js, Postgres). Each has a
stable `P-NNN` id that reviewers cite. They load in tiers: every run gets a short index of the
vital few plus a category table (about 720 tokens), and a skill pulls a category or a full principle
only when it is relevant. A CI ratchet keeps that index small; see
[`docs/instruction-density-budget.md`](instruction-density-budget.md).
