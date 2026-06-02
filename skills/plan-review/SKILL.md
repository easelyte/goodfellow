---
name: plan-review
description: Adversarial plan review with research injection — verifies factual claims via web search, then runs multi-round review with verifier pass. Research-then-adversarial grounds critics in verified facts.
---

Run a multi-round adversarial review on the plan file the operator indicated.

## 0. Read the plan

Read the plan file fully. Also read its spec (from plan frontmatter). Read `.shipline/knowledge.md` if it exists.

## 1. Research injection (before adversarial rounds)

Extract factual, externally verifiable claims from the plan:
- Library/framework version claims
- API endpoint behavior
- Tool availability and flags
- Rate limits, quotas, TTLs

Announce: "Researching N claims: [summary]. WebSearch dispatched."

Dispatch parallel WebSearch calls. Append to plan:

```
## Appendix: Verified Claims (research pass YYYY-MM-DD)

✓ Claim: <text>. Verified: <source URL>.
✗ Claim: <text>. REFUTED: <correct fact + source>. Plan needs revision.
? Claim: <text>. No clear evidence — flagged for reviewers.
```

If any refuted: surface to operator. Otherwise proceed to adversarial.

**Graceful degradation:** if WebSearch unavailable, skip. Log reason. Proceed to adversarial.

## 2. Adversarial loop (same structure as spec-review)

Each round, dispatch both reviewers in parallel:

**Reviewer 1 (Claude subagent, model: sonnet or SHIPLINE_REVIEW_MODEL):**

> "You are an adversarial plan reviewer. Read <path>. Find: missing prerequisites, wrong execution order, unaddressed dependencies, steps that will fail, missing tests, missing rollback paths, risky API assumptions. Challenge '?' claims in the Verified Claims appendix. Output: ## Verdict / ## Blockers / ## Major / ## Minor."

**Reviewer 2 (Codex bridge):**

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/codex-bridge.sh" --kind plan --uncommitted
```

## 3. Reconcile + address (no gate rounds 1-3)

Same flow as spec-review: deduplicate, present, fix blockers + majors, re-dispatch.

## 4. Verifier pass (round 2+)

Before fixing, verify each finding is still real against current plan state.

## 5. Convergence

Same rules as spec-review. Hard cap 6. Deferred findings: discard (plan-review doesn't file loops).

## 6. After convergence

Summarize: "Plan converged at round N. Research verified X/Y claims, refuted Z."

Auto-dispatch `/shipline:execute <plan-path>`.
