# Changelog

All notable changes to Goodfellow are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the version is `0.x`,
a minor bump may include breaking changes; they are always listed under **Changed** or **Removed**.

Versions `0.1.0` and `0.2.0` were declared in the plugin manifest but never tagged; the first tagged
release will be the first version below `[Unreleased]`. See [RELEASING.md](RELEASING.md).

## [Unreleased]

### Added

- **Tests that can fail.** `plan` names each behaviour task's expected red (an assertion message,
  never "function not defined") and, on high-stakes paths, the fail-closed branches, boundaries and
  one deliberate break per rule. `execute` is test-first with a right-reason red and never edits an
  expected value to match output. The Codex diff reviewer gained a test-quality block, and the plan
  reviewer flags high-stakes tasks with no expected red. New principles P-094 to P-096
  ([#30](https://github.com/easelyte/goodfellow/pull/30)).
- **`scripts/red_check.py`.** Replays a branch's new tests against the base in a temporary worktree
  and requires an assertion failure there and a pass on the branch (`OK`, `WRONG_REASON`, `NOT_RED`,
  `NOT_GREEN`, `NEW_SYMBOL`). Runner-agnostic via JUnit XML; runs in `ship` by default
  ([#30](https://github.com/easelyte/goodfellow/pull/30)).
- **`scripts/mutation_check.py`.** Opt-in, diff-scoped mutation testing of the Python lines a branch
  changed in files listed in `.goodfellow/high_stakes_paths.txt`, in throwaway copies and under a
  time budget. Running out of budget is reported as incomplete, never as a pass. Code that sends
  signals, spawns processes, or deletes or writes files is only mutated inside a PID namespace
  (`--isolated`) or with fakes (`--fakes`). Every test run gets its own process group, killed on
  timeout, exit and termination signals ([#30](https://github.com/easelyte/goodfellow/pull/30)).
- **Tool-layer guards.** A `PreToolUse` hook (`scripts/guard_engine.py`) blocks `git add -A`/`.`/
  `--all`, `--dangerously-skip-permissions`, and force-pushes to `main`/`master` by default, plus
  any BLOCK rules a project declares in `.goodfellow/guards.json`. `--validate` for CI and
  `--selfcheck` to print the enforced set; `snap-compact` re-asserts the set after compaction
  ([#26](https://github.com/easelyte/goodfellow/pull/26)).
- **Tiered principle index.** The always-loaded index carries the vital-few principles plus a
  category routing table; `--category NAME` and `--show P-NNN` load more on demand. The core index
  dropped from about 2,800 to about 720 tokens. 14 new seed principles, P-080 to P-093
  ([#28](https://github.com/easelyte/goodfellow/pull/28)).
- **Progressive disclosure for seeded principles** and a CI density ratchet
  (`measure_principle_density.py`, `docs/instruction-density-budget.md`) that caps what is injected
  on every run ([#24](https://github.com/easelyte/goodfellow/pull/24)).
- **Parallel implementers in `execute`.** A phase's independent tasks can fan out to parallel
  implementer agents, each in its own runtime-isolated git worktree, reconciled by merge. Serial
  remains the default ([#13](https://github.com/easelyte/goodfellow/pull/13),
  [#23](https://github.com/easelyte/goodfellow/pull/23)).
- **Two-stage generator and judge review.** On the Codex path a generator emits structured findings
  and a judge grounds or drops each one; judge failures fail open to the unjudged findings with a
  banner. Optional static-analysis pre-pass (`ruff`, `shellcheck`, `gitleaks`, `semgrep`,
  auto-detected) ([#16](https://github.com/easelyte/goodfellow/pull/16)).
- **Judge lens tag.** Each judged finding can carry a reviewer lens, threaded through `loops.json`
  (`--lens`) and lens-tuning attribution ([#25](https://github.com/easelyte/goodfellow/pull/25)).
- **`public-pr` skill** and `scripts/public_pr_scrub.py`: a pre-open gate for PRs to public or
  upstream repositories, with a configurable internal-reference scrub and correct cross-fork
  `gh pr create` flags ([#16](https://github.com/easelyte/goodfellow/pull/16)).
- **`grill` skill.** An opt-in, one-question-at-a-time interview for fuzzy or high-stakes design
  intent that stops when its open-decision ledger is empty
  ([#9](https://github.com/easelyte/goodfellow/pull/9),
  [#10](https://github.com/easelyte/goodfellow/pull/10)).
- **Reviewer lenses.** `spec-review` and `plan-review` give their two reviewers different lenses,
  batch the verifier pass, and run research in an isolated subagent
  ([#14](https://github.com/easelyte/goodfellow/pull/14)).
- **Lens-tuning report** (`scripts/lens_tuning.py`): a read-only pointer to reviewer sources whose
  findings mostly triage as not-a-defect ([#18](https://github.com/easelyte/goodfellow/pull/18)).
- **Rollback journal and evidence provenance** for the rich memory backend, completed as a
  two-phase (intent, commit) write-ahead log with byte-bound crash recovery
  ([#19](https://github.com/easelyte/goodfellow/pull/19),
  [#22](https://github.com/easelyte/goodfellow/pull/22)).
- **Durable per-loop `uuid`** so loop references do not alias after `loops.json` is reset
  ([#21](https://github.com/easelyte/goodfellow/pull/21)).
- **P-079, "Reaching a limit is not success".** A cap, budget or timeout halt is reported as a halt
  in prose and in control flow across the chain ([#17](https://github.com/easelyte/goodfellow/pull/17)).
- **Recommended defaults** for each `brainstorm` clarifying question
  ([#11](https://github.com/easelyte/goodfellow/pull/11)).
- **Parallel triage reviewers**, dispatched in one batch per backlog
  ([#12](https://github.com/easelyte/goodfellow/pull/12)).
- **Marketplace manifest** (`.claude-plugin/marketplace.json`), so the plugin installs directly with
  `/plugin marketplace add easelyte/goodfellow`, and a CI version-consistency check
  ([#6](https://github.com/easelyte/goodfellow/pull/6)).
- **Seed principles** P-059, P-061, P-063 to P-070 and sub-entries P-017a/P-017b.
- **Brand assets:** README hero banner, pipeline diagram and a square plugin icon
  ([#8](https://github.com/easelyte/goodfellow/pull/8),
  [#29](https://github.com/easelyte/goodfellow/pull/29)).
- **Release process:** Keep a Changelog format, a tag-triggered release workflow that publishes the
  matching CHANGELOG section as the GitHub Release notes, `RELEASING.md`, `CONTRIBUTING.md`,
  `SECURITY.md`, and issue and pull request templates.

### Changed

- With no Codex CLI, the fallback reviewer now defaults to `opus` instead of `sonnet`, so the only
  reviewer is not weaker than the model that wrote the code. `GOODFELLOW_REVIEW_MODEL` still
  overrides it ([#25](https://github.com/easelyte/goodfellow/pull/25)).
- `ship` routes review findings by three tiers: blockers stop the PR, majors are filed as loops,
  minors become knowledge gotchas ([#20](https://github.com/easelyte/goodfellow/pull/20)).
- `pyproject.toml` version aligned with `plugin.json` at `0.2.0`
  ([#6](https://github.com/easelyte/goodfellow/pull/6)).

### Fixed

- **Guard engine fail-open gaps.** Mutation testing found `git add` forms the guard let through; it
  now also blocks `-A` bundled into other short flags (`-fA`), the whole-tree pathspecs `./`, `*`,
  `:/` and `:(top)`, pathspecs that are all exclusions (`git add ':!secrets.env'` adds everything
  else) and `--pathspec-from-file`. Git run through a relative path without a leading `./`
  (`usr/bin/git`) is recognised, and Windows drive paths with forward slashes compare
  case-insensitively ([#32](https://github.com/easelyte/goodfellow/pull/32)).
- **`public_pr_scrub.py` could scan an empty diff.** Without `--base` it used the branch's upstream,
  which for a pushed feature branch is itself, so the whole PR went unscanned. It now scans against
  every likely PR target that exists (`upstream/HEAD`, `upstream/main`, `upstream/master`,
  `origin/HEAD`, `origin/main`, `origin/master`, `main`, `master`), skipping any that already contain
  `HEAD`. With no base left it fails closed (exit 2) instead of diffing against the tip's parent;
  a missing `git` or an uncomputable merge-base (a shallow clone) is also exit 2. Pass `--base` to
  scan against the exact target ([#32](https://github.com/easelyte/goodfellow/pull/32)).
- The scrub's added-line parser tracks diff hunks, so a content line starting `++` is scanned instead
  of being mistaken for a file header, and it reads a plain diff (`--no-color --no-ext-diff`), so
  `color.diff=always` can no longer hide every added line
  ([#32](https://github.com/easelyte/goodfellow/pull/32)).
- Major review findings were silently dropped at ship time; they now reach `loops.json`
  ([#20](https://github.com/easelyte/goodfellow/pull/20)).
- The review bridge exits with a `REVIEW_FAILED` sentinel on any nonzero exit, so a review that dies
  mid-run can no longer read as a clean pass ([#15](https://github.com/easelyte/goodfellow/pull/15)).
- Codex was invoked with a Claude model name; the Codex path now takes its model only from
  `GOODFELLOW_CODEX_MODEL` ([#7](https://github.com/easelyte/goodfellow/pull/7)).
- Spec and plan review saw an empty context for a freshly written, untracked file; the bridge now
  embeds the file body ([#7](https://github.com/easelyte/goodfellow/pull/7)).
- `spec-review` ignored `next_action: halt-after-spec-review` in the spec frontmatter
  ([#7](https://github.com/easelyte/goodfellow/pull/7)).
- `grill` validated its topic slug before using it in a path, closing a path traversal
  ([#10](https://github.com/easelyte/goodfellow/pull/10)).

## [0.2.0] - 2026-06-11

Seeded knowledge and an opt-in rich memory backend.

### Added

- **Seeded universal design principles.** `knowledge/principles.md` (47 stack-agnostic principles)
  and `knowledge/principles-web.md` (8 JS/React/Next.js/Postgres/RLS rules, opt-in). Plugin-owned
  and read-only; chain skills cite violations by stable `P-NNN` id. The web supplement is enabled by
  `GOODFELLOW_PRINCIPLES_WEB=1` or an auto-detected `package.json`
  ([#4](https://github.com/easelyte/goodfellow/pull/4)).
- **Opt-in rich memory backend (`GOODFELLOW_MEMORY=rich`).** Per-fact files, a regenerated index and
  domain registries, with atomic, locked, transactional writes and a crash-resumable migration from
  the flat file. `flat` remains the zero-config default
  ([#5](https://github.com/easelyte/goodfellow/pull/5)).
- New configuration: `GOODFELLOW_PRINCIPLES_WEB`, `GOODFELLOW_MEMORY`, `GOODFELLOW_MEMORY_WARN_KB`,
  all failing loud on invalid values.
- CI lints shell scripts with `bash -n` and `shellcheck`
  ([#2](https://github.com/easelyte/goodfellow/pull/2)).
- `scripts/run_log.sh` gives autopilot decision logs a concrete path under `.goodfellow/runs/`
  ([#3](https://github.com/easelyte/goodfellow/pull/3)).

### Changed

- The self-review step of `spec-review` and `plan-review` applies only small, unambiguous fixes
  before reviewers see the document, so a rewrite cannot slip past them
  ([#1](https://github.com/easelyte/goodfellow/pull/1)).

### Fixed

- `loop_store.py` rejects priorities outside `p1` to `p4`
  ([#2](https://github.com/easelyte/goodfellow/pull/2)).
- Dry-run autopilot no longer edits the spec or plan file (the research appendix and self-review
  fixes are logged instead), and a research match is labelled "relevant source found" rather than
  "verified" ([#1](https://github.com/easelyte/goodfellow/pull/1),
  [#3](https://github.com/easelyte/goodfellow/pull/3)).
- The README no longer claims autopilot halts that are not wired into the chain
  ([#2](https://github.com/easelyte/goodfellow/pull/2)).

## [0.1.0] - 2026-06-02

Initial release.

### Added

- 12 skills: `brainstorm`, `spec-review`, `plan`, `plan-review`, `execute`, `ship`,
  `codex-review`, `triage`, `snap-compact`, `close`, `branch`, `prune-stale`.
- Knowledge compounding loop (`.goodfellow/knowledge.md`).
- Follow-up loop tracking (`.goodfellow/loops.json`).
- Multi-model adversarial review (Claude plus Codex), with a single-Claude fallback.
- Research injection: web-search verification of load-bearing claims.
- Verifier pass for round 2 and later findings.
- Autopilot mode with dry-run.
- Triage with two-reviewer reconciliation.

[Unreleased]: https://github.com/easelyte/goodfellow/commits/main
[0.2.0]: https://github.com/easelyte/goodfellow/commits/136f429
[0.1.0]: https://github.com/easelyte/goodfellow/commits/48af7a9
