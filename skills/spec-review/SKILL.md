---
name: spec-review
description: Multi-round adversarial spec review with research injection, verifier pass, and knowledge-file principle checking. Dispatches two reviewers per round (Claude + Codex, or single Claude fallback).
---

Run a multi-round adversarial review on the spec file the operator indicated.

## 0. Read the spec

Read the spec file fully. Also read `.goodfellow/knowledge.md` (Principles + Gotchas sections) if it exists — these inform principle checking during review.

Also read the plugin-shipped universal design principles and flag violations by their `P-NNN` id (the web supplement is read only when web context is opted in — `GOODFELLOW_PRINCIPLES_WEB=1` or a `package.json` at the project root; an invalid value hard-errors here):

```bash
for f in $(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/principles_context.py" --project-root .); do
  cat "${CLAUDE_PLUGIN_ROOT}/knowledge/$f"
done
```

## 0.5 Parent self-review (Opus pass)

First, initialize the run log so any decision below has a concrete destination:

```bash
RUN_LOG=$(bash "${CLAUDE_PLUGIN_ROOT}/scripts/run_log.sh")
```

This creates `.goodfellow/runs/` (idempotent) and resolves a concrete timestamped path (e.g. `./.goodfellow/runs/20260604T173000Z.jsonl`). Use `$RUN_LOG` for **every** append in this skill — never write to a literal `<timestamp>.jsonl` placeholder. (The file is created on first append; interactive runs that log nothing leave none.)

Then do your own review pass as the parent model. Look for:
- Internal contradictions (section A says X, section B says not-X)
- Undefined behavior at decision boundaries
- Success criteria that can't be tested
- Knowledge gotcha violations (if `.goodfellow/knowledge.md` exists)

Dispose of each finding by class — **apply small unambiguous fixes only; NO large structural rewrite in this pass:**
- **Small + unambiguous** (typo, dangling reference, single-line clarification) → fix inline now. This pass is cheap (no subagent cost) and clears low-hanging fruit that would otherwise consume a full review round.
- **Large or ambiguous** (structural rewrite, a contradiction whose correct resolution isn't obvious) → do NOT fix. Surface it and defer to the reviewer rounds. A larger-but-seemingly-correct restructuring done here rides into the reviewers unchallenged — blind-rewriting a contradiction's baseline before reviewers see it removes their chance to catch and revert it.
- **Needs an operator decision** (a scope question only the operator can settle) → strategic halt. Stop the chain; under autopilot append `{"event": "self_review_halt", "reason": "<the question>"}` to `$RUN_LOG`. Do not guess the operator's intent.

**Autopilot dry-run (`GOODFELLOW_AUTOPILOT=dry-run`):** don't apply self-review fixes inline. For each small-unambiguous fix you would make, log `{"event": "self_review_fix", "would_act": true, "fix": "<one-line>"}` to `$RUN_LOG` instead of editing. (Large/ambiguous findings carry to the reviewer rounds as usual; halts log as above. This branch scopes only the step-0.5 edits — the research-injection append has its own dry-run branch in step 1.5.)

## 1. Each round, dispatch both reviewers in parallel

**Reviewer 1 (Claude subagent, model from GOODFELLOW_REVIEW_MODEL or default sonnet):**

Use the Agent tool with `model: "sonnet"` (or the value of GOODFELLOW_REVIEW_MODEL). Prompt:

> "You are an adversarial spec reviewer. Read <path>. Find weaknesses: contradictions, undefined behavior, missing requirements, ambiguous success criteria, hidden coupling. Check against .goodfellow/knowledge.md principles and gotchas if provided. Output: ## Verdict / ## Blockers / ## Major / ## Minor. Per-finding: cite section, explain issue, state fix. If a finding matches a knowledge gotcha, note 'knowledge-elevated' and bump severity one tier (cap at blocker)."

**Reviewer 2 (Codex bridge):**

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/codex-bridge.sh" --kind spec --uncommitted
```

If Codex is absent, the bridge falls back to a single Claude reviewer automatically.

## 1.5 Research injection (between round 1 and round 2 only)

After round 1 findings return, extract load-bearing factual claims:
- API/library existence and behavior
- Version compatibility
- Protocol/standard support

Verify via Tavily batch search (if `GOODFELLOW_TAVILY_KEY` is set) or WebSearch fallback (cap: 5 searches):

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/research.sh" --claims '<json array of claims>' --max 5
```

Append the results to review context. The adapter marks each claim:
- **Source-matched (✓):** a relevant source was found (word-overlap heuristic) — this means *a source talks about the claim*, NOT that the claim was confirmed or agreed with. Include the source URL.
- **No clear source (?):** nothing relevant surfaced (flagged for reviewers).

Note: the Tavily adapter scores relevance only — a contradicting source scores the same as a confirming one, so ✓ is **not** a verification verdict and there is no refutation signal. Read ✓ sources manually before relying on a claim.

**Autopilot dry-run (`GOODFELLOW_AUTOPILOT=dry-run`):** do NOT append the appendix to the spec file. Instead log `{"event": "would_append_verified_claims", "would_act": true, "claims": <n>, "source_matched": <n>, "no_source": <n>}` to `$RUN_LOG` (from step 0.5). The dry-run contract is observe-without-mutating; the appendix is a project-file mutation.

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
