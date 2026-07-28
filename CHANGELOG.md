# Changelog

## Unreleased

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
