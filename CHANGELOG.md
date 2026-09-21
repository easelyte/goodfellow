# Changelog

## Unreleased

- **Worktree-isolated parallel implementers.** `execute` can fan out a phase's independent tasks
  across parallel implementer agents, each in its own runtime-isolated git worktree branched off a
  checkpoint commit, with results reconciled by merge. Because no two children write the same working
  tree, concurrent implementers can't clobber or strand each other's commits — isolation is enforced
  at the runtime layer, not by a prose instruction to the child, and file overlap becomes a
  merge-cleanliness hint rather than a corruption hazard. Serial stays the default; fan-out is the
  justified exception (floor ~3 genuinely independent tasks, sized against the runtime concurrency
  cap), and `execute` falls back to serial when runtime-enforced isolation isn't available.
- **Progressive disclosure for seeded principles.** Chain runs now inject only the principle INDEX
  (each `P-NNN` id + title + one-line rule) and pull full bodies on demand via `--show P-NNN`,
  mirroring the Agent Skills loading model. Cuts the always-loaded principle footprint from ~17k to
  ~2.8k tokens (core corpus). A CI density ratchet (`measure_principle_density.py`) keeps the
  always-injected index under a research-derived cap, so principle growth displaces rather than
  accumulates. Budget, growth rule, and placement documented in `docs/instruction-density-budget.md`.
- **Judge lens tag + stronger no-Codex reviewer default.** The judge decision object gains an
  optional, fail-open `lens` field threaded through the validator, `review_judge`, loop store
  (`--lens`), and per-lens tuning attribution — turning reviewer-lens tuning from prose-only into
  measurable, with an explicit `other` (measured) distinguished from missing/malformed lens
  (`unattributed`, no-data) so absent provenance can't emit a false tuning signal. Separately, the
  no-Codex fallback reviewer now defaults to the stronger model, so the fallback path (which has no
  cross-family reviewer) is no longer a strength inversion; the Codex-present path is unchanged.
- **Tool-layer enforcement guards (PreToolUse).** A new `PreToolUse` hook (`hooks/hooks.json` →
  `scripts/guard_engine.py`) enforces expensive-to-reverse constraints at the tool layer instead of
  in prose a compaction can silently drop. Ships three built-in universal guards on by default (no
  project knowledge required): `git add -A`/`.`/`--all`, the `--dangerously-skip-permissions` CLI
  flag, and force-push to a protected branch (`main`/`master`; feature branches unaffected). Matching
  is shlex-token based and built-ins inspect only the `Bash` command, so writing or documenting a
  blocked flag in a file — or mentioning it inside a quoted commit message — never trips a guard.
  Projects add their own BLOCK rules in `.goodfellow/guards.json` (`substring`/`regex` match,
  per-tool scoping, per-rule `bypass_env`; see `configs/guards.example.json`). Denies via the
  documented `permissionDecision` JSON contract (asserted by JSON, not exit code). Fails *safe-open*
  on a malformed config (built-ins still enforce, no deadlock); `guard_engine.py --validate` fails
  *loud* for CI, and `--selfcheck` prints the enforced set. The `snap-compact` skill now snapshots
  that set and re-asserts it after the compaction boundary. Toggles: `GOODFELLOW_GUARDS=0`
  (built-ins off), `CLAUDE_HOOK_BYPASS=1` (all off, one command).
- **New `grill` skill — opt-in relentless-interview design front-end.** A sibling to `brainstorm`
  for fuzzy or high-stakes intent: a bounded fact-scout (≤8 tool-calls, foreground), then a
  one-question-at-a-time interview (each question ships a recommended default + a prominent "enough /
  write it" escape hatch, tracked against an understanding ledger) that self-terminates when the
  open-decision ledger is empty — no hard question cap. Writes the spec via an atomic no-clobber
  publish (collision → disambiguated `-2`/`-3` path, never an overwrite), persists durable
  pending-review recovery frontmatter (`review_status`/`failed_reviewers`/`resume`) up front, and
  auto-dispatches spec-review by file content. Explicit-invocation only (`/goodfellow:grill`, "grill
  me on X", "interview me about X") — never auto-selected over `brainstorm`. Three-state autopilot:
  `=1` writes-from-context with `confidence: low` + `next_action: halt-after-spec-review`; `dry-run`
  writes no spec and dispatches no review, but does append `would_act` events to the run log
  (`.goodfellow/runs/`). Carries a `CONTRACT-SYNC` marker for future cross-repo
  contract-parity checking. Interview philosophy adapted from Matt Pocock's `grilling` skill.
- **Expanded seed principles.** Core `knowledge/principles.md` grows to 56 principles + 5 sub-entries (added P-059, P-061, P-063–P-069, and sub-entries P-017a/P-017b); web `knowledge/principles-web.md` grows to 10 (added P-062, P-070). Ported from easelyte's cross-repo design knowledge and grounded against current industry practice (OWASP, capability-based security / dual-LLM prompt-injection defense, ReDoS / algorithmic-complexity attacks, design-token semantics, git squash-merge semantics, optimistic-UI last-write-wins). P-060 intentionally skipped (worktree/canonical-store infra, out of scope for a general code-shipping tool — consistent with the existing 039/041/043 gaps). IDs stay aligned with the upstream `P-NNN` numbering; all KB contract tests pass.

## 0.2.0 (2026-06-11)

Seeded knowledge + opt-in rich memory backend.

- **Seeded universal design principles.** Ships `knowledge/principles.md` (47 stack-agnostic principles) + `knowledge/principles-web.md` (8 JS/React/Next.js/Postgres/RLS rules, opt-in). Plugin-owned and read-only; the chain skills read them every run and cite violations by stable `P-NNN` ids, so a fresh install starts with accumulated wisdom instead of an empty knowledge file. Web supplement enabled via `GOODFELLOW_PRINCIPLES_WEB=1` or an auto-detected `package.json`. Public-egress-guarded in CI.
- **Opt-in rich memory backend (`GOODFELLOW_MEMORY=rich`).** Per-fact files (`.goodfellow/memory/*.md`) + a regenerated index (`.goodfellow/MEMORY.md`) + domain registries, with atomic/locked/transactional writes, crash-resumable flat→rich migration, and hybrid recall. `flat` (append-only `.goodfellow/knowledge.md`) remains the zero-config default and is unchanged.
- **New config:** `GOODFELLOW_PRINCIPLES_WEB`, `GOODFELLOW_MEMORY`, `GOODFELLOW_MEMORY_WARN_KB` — all fail-loud on invalid values.

## 0.1.0 (2026-06-02)

Initial release.

- 12 skills: brainstorm, spec-review, plan, plan-review, execute, ship, codex-review, triage, snap-compact, close, branch, prune-stale
- Knowledge compounding loop (.goodfellow/knowledge.md)
- Follow-up loop tracking (.goodfellow/loops.json)
- Multi-model adversarial review (Claude + Codex/GPT)
- Research injection (web search verification of load-bearing claims)
- Verifier pass for round 2+ findings
- Autopilot mode with dry-run
- Triage system with two-reviewer reconciliation
