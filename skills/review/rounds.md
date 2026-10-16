## 3. Each round, two reviewers in parallel

Both emit the same format (`## Verdict / ## Blockers / ## Major / ## Minor`; per finding: cite the
section, explain the issue, state the fix). Their lenses differ, so the second slot buys coverage,
not duplicate hunting.

**Reviewer 1 (Claude subagent, `GOODFELLOW_REVIEW_MODEL` or `sonnet`): testability and requirements.**

> "You are an adversarial <spec|plan> reviewer with a **testability, acceptance-criteria, requirements-completeness, and principle-compliance lens**. Read <path>. Focus on: criteria that can't be objectively tested; requirements that are incomplete, ambiguous or missing; <for a plan: spec requirements with no task, tasks with ambiguous done-criteria, missing tests and expected reds>; and compliance with the seeded principles (cite P-NNN) plus .goodfellow/knowledge.md. Challenge '?' claims in any Researched Claims appendix. Output: ## Verdict / ## Blockers / ## Major / ## Minor. If a finding matches a knowledge gotcha, note 'knowledge-elevated' and bump severity one tier (cap at blocker)."

**Reviewer 2 (Codex bridge): correctness.** Use `--file`: a fresh document is usually untracked,
so a diff-scoped review would see nothing. The lens rides in the trailing prompt, so the Claude
fallback (no Codex installed) still applies it.

```bash
KIND=spec   # or: KIND=plan
bash "${CLAUDE_PLUGIN_ROOT}/scripts/codex-bridge.sh" --kind "$KIND" --file <path> \
  -- "Review this $KIND with a correctness, security, edge-cases, hidden-coupling, and contract-integrity lens. Focus on: logical errors, security exposure, unhandled failure modes, hidden coupling, contract integrity; for a plan also missing prerequisites, wrong execution order, steps that will fail at runtime and missing rollback paths. Challenge '?' claims in any Researched Claims appendix. Output: ## Verdict / ## Blockers / ## Major / ## Minor. Per-finding: cite section, explain issue, state fix."
```

**Failed-review contract:** on success the bridge prints an artifact path; on failure it exits
nonzero and prints `REVIEW_FAILED <code> <class>`. Reject that prefix before any read. A failed
review is a failed round, never an empty pass; stop the round and surface it.

## 4. Verify, reconcile, fix

- **From round 2, verify first.** One batched verifier subagent gets all of the round's findings
  (numbered) and the current document, and returns `real` / `stale` / `noise` per finding. Only
  `real` findings are fixed.
- Deduplicate across reviewers; note agreements (high confidence) and disagreements.
- Fix every blocker and major in the document, then run the next round. No "how do you want to
  proceed?" between rounds. Stop and ask only when a blocker shows the document's core model is
  wrong and the fix is a scope decision (a product call).

