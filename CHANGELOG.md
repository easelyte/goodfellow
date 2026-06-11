# Changelog

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
