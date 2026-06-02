---
name: spec-review
description: Multi-round adversarial spec review with research injection, verifier pass, and knowledge-file principle checking. Dispatches two reviewers per round (Claude + Codex or dual-Claude fallback).
---

Run a multi-round adversarial review on the spec file the operator indicated.

## 0. Read the spec

Read the spec file fully. Also read `.goodfellow/knowledge.md` (Principles + Gotchas sections) if it exists — these inform principle checking during review.

## 1. Each round, dispatch both reviewers in parallel

**Reviewer 1 (Claude subagent, model from GOODFELLOW_REVIEW_MODEL or default sonnet):**

Use the Agent tool with `model: "sonnet"` (or the value of GOODFELLOW_REVIEW_MODEL). Prompt:

> "You are an adversarial spec reviewer. Read <path>. Find weaknesses: contradictions, undefined behavior, missing requirements, ambiguous success criteria, hidden coupling. Check against .goodfellow/knowledge.md principles and gotchas if provided. Output: ## Verdict / ## Blockers / ## Major / ## Minor. Per-finding: cite section, explain issue, state fix. If a finding matches a knowledge gotcha, note 'knowledge-elevated' and bump severity one tier (cap at blocker)."

**Reviewer 2 (Codex bridge):**

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/codex-bridge.sh" --kind spec --uncommitted
```

If Codex is absent, the bridge falls back to dual-Claude automatically.

## 1.5 Research injection (between round 1 and round 2 only)

After round 1 findings return, extract load-bearing factual claims:
- API/library existence and behavior
- Version compatibility
- Protocol/standard support

Verify via WebSearch (cap: 5 searches). Append verified claims to review context:
- **Confirmed:** claim + source URL
- **Contradicted:** correct fact + source
- **Unverifiable:** no authoritative source

**Graceful degradation:** if WebSearch is unavailable, skip silently. Log "research injection skipped: <reason>". All findings retain original severity.

## 2. Reconcile findings

- Deduplicate across reviewers
- Note agreements (high confidence) vs disagreements (judgment call)
- Present: Blockers, Major, Minor with reviewer attribution

## 3. Address findings (no gate rounds 1-3)

After presenting findings, in the same turn:
1. Revise the spec to fix every blocker and major
2. Re-dispatch round N+1

No trailing question. No "How do you want to proceed?"

**Round 4+:** present findings and ask once whether to continue or ship.

**Paradigm-shift carve-out:** gate ONLY when a blocker reveals the spec's core mental model is wrong and fixing it requires a scope decision.

## 4. Verifier pass (round 2+)

Before fixing round 2+ findings, dispatch a lightweight verifier for each finding:

The verifier checks: does this finding still apply to current code? Returns `real`/`stale`/`noise`.

Only `real` findings proceed to fix. Stale/noise get noted but not fixed.

## 5. Convergence

Declare convergence when new findings drop from safety-critical to polish-tier.

**Hard cap:** 6 rounds. At hard cap:
- Safety-critical findings remain → halt, recommend rewrite
- Only non-blocking findings → declare convergence, note deferred findings
- No findings → converge

**Confidence promotion:** if spec-review resolves all architecture-changing unresolved_questions, update `confidence:` in spec frontmatter from `low` to `medium` or `high`.

## 6. After convergence

Summarize: "Spec converged at round N. Key changes: {bullets}."

Deferred findings: discard (spec-review doesn't file loops — unresolved findings are addressed in the next chain stage).

Auto-dispatch `/goodfellow:plan <spec-path>`.
