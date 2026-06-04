---
name: plan-review
description: Adversarial plan review with research injection — verifies factual claims via web search, then runs multi-round review with verifier pass. Research-then-adversarial grounds critics in verified facts.
---

Run a multi-round adversarial review on the plan file the operator indicated.

## 0. Read the plan

Read the plan file fully. Also read its spec (from plan frontmatter). Read `.goodfellow/knowledge.md` if it exists.

## 0.5 Parent self-review (Opus pass)

Before dispatching external reviewers, do your own review pass as the parent model. Look for:
- Missing dependencies (task A needs B but the graph doesn't show it)
- Wrong execution order (task references something built in a later phase)
- Spec-coverage gaps (spec section with zero plan tasks)
- Acceptance criteria that are untestable or contradict each other
- Knowledge gotcha violations

Dispose of each finding by class — **apply small unambiguous fixes only; NO large structural rewrite in this pass:**
- **Small + unambiguous** (typo, dangling reference, single-line clarification) → fix inline now. This pass is free and clears low-hanging fruit that would otherwise dominate round 1 findings.
- **Large or ambiguous** (re-sequencing phases, a dependency rework whose correct shape isn't obvious) → do NOT fix. Surface it and defer to the reviewer rounds. A larger-but-seemingly-correct restructuring done here rides into the reviewers unchallenged, where a blind rewrite can no longer be caught and reverted.
- **Needs an operator decision** (a scope question only the operator can settle) → strategic halt. Stop the chain; under autopilot append `{"event": "self_review_halt", "reason": "<the question>"}` to `.goodfellow/runs/<timestamp>.jsonl`. Do not guess the operator's intent.

**Autopilot dry-run (`GOODFELLOW_AUTOPILOT=dry-run`):** do NOT mutate the plan — dry-run observes without writing. For each small-unambiguous fix you would apply, log `{"event": "self_review_fix", "would_act": true, "fix": "<one-line>"}` to `.goodfellow/runs/<timestamp>.jsonl` instead of editing. Deferrals and halts log as above.

## 1. Research injection (before adversarial rounds)

Extract factual, externally verifiable claims from the plan:
- Library/framework version claims
- API endpoint behavior
- Tool availability and flags
- Rate limits, quotas, TTLs

Announce: "Researching N claims: [summary]."

Verify via Tavily batch search (if `GOODFELLOW_TAVILY_KEY` is set) or WebSearch fallback:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/research.sh" --claims '<json array of claims>' --max 5
```

Append to plan:

```
## Appendix: Verified Claims (research pass YYYY-MM-DD)

✓ Claim: <text>. Verified: <source URL>.
? Claim: <text>. No clear evidence — flagged for reviewers.
```

Note: the Tavily adapter uses a word-overlap heuristic and cannot detect outright refutation — only confirmation (✓) or no-clear-evidence (?). If a ✓ claim contradicts your expectation, read the cited source manually. Proceed to adversarial after appending the appendix.

**Graceful degradation:** if WebSearch unavailable, skip. Log reason. Proceed to adversarial.

## 2. Adversarial loop (same structure as spec-review)

Each round, dispatch both reviewers in parallel:

**Reviewer 1 (Claude subagent, model: sonnet or GOODFELLOW_REVIEW_MODEL):**

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

Summarize: "Plan converged at round N. Research verified X/Y claims, Z unverifiable." (The Tavily adapter has no refutation path — never report a "refuted" count.)

Auto-dispatch `/goodfellow:execute <plan-path>`.
