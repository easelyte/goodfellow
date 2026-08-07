---
name: plan-review
description: Adversarial plan review with research injection — verifies factual claims via web search, then runs multi-round review with verifier pass. Research-then-adversarial grounds critics in verified facts.
---

Run a multi-round adversarial review on the plan file the operator indicated.

## 0. Read the plan

Read the plan file fully. Also read its spec (from plan frontmatter). Then read the project's accumulated knowledge, backend-aware (invalid `GOODFELLOW_MEMORY` hard-errors here):

```bash
MODE=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory_config.py" resolve-mode) || { echo "$MODE"; exit 1; }
if [ "$MODE" = "rich" ]; then
  # Full MEMORY.md index (incl. ## Pending (unconfirmed) — discount those). Internally
  # falls back: .migrating -> knowledge.md (no regen), absent -> knowledge.md, dirty/stale -> regenerate.
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory_index.py" --root .goodfellow read-index
else
  cat .goodfellow/knowledge.md 2>/dev/null || true   # flat
fi
```

In rich mode, auto-pull bodies of exact-`domain` matches; open other relevant fact bodies by name from the index.

Also read the plugin-shipped universal design principles and flag violations by their `P-NNN` id (the web supplement is read only when web context is opted in — `GOODFELLOW_PRINCIPLES_WEB=1` or a `package.json` at the project root; an invalid value hard-errors here):

```bash
# One robust command: resolves + reads the seeded principles, with ALL error handling
# in Python (bad config / missing core / unreadable file -> non-zero exit + stderr).
# Its stdout IS the principles to apply (cite violations by P-NNN). A non-zero exit
# means a config/packaging problem — stop and surface it.
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/principles_context.py" --emit --project-root .
```

## 0.5 Parent self-review (Opus pass)

First, initialize the run log so any decision below has a concrete destination:

```bash
RUN_LOG=$(bash "${CLAUDE_PLUGIN_ROOT}/scripts/run_log.sh")
```

This creates `.goodfellow/runs/` (idempotent) and resolves a concrete timestamped path (e.g. `./.goodfellow/runs/20260604T173000Z.jsonl`). Use `$RUN_LOG` for **every** append in this skill — never write to a literal `<timestamp>.jsonl` placeholder. (The file is created on first append; interactive runs that log nothing leave none.)

Then do your own review pass as the parent model. Look for:
- Missing dependencies (task A needs B but the graph doesn't show it)
- Wrong execution order (task references something built in a later phase)
- Spec-coverage gaps (spec section with zero plan tasks)
- Acceptance criteria that are untestable or contradict each other
- Knowledge gotcha violations

Dispose of each finding by class — **apply small unambiguous fixes only; NO large structural rewrite in this pass:**
- **Small + unambiguous** (typo, dangling reference, single-line clarification) → fix inline now. This pass is free and clears low-hanging fruit that would otherwise dominate round 1 findings.
- **Large or ambiguous** (re-sequencing phases, a dependency rework whose correct shape isn't obvious) → do NOT fix. Surface it and defer to the reviewer rounds. A larger-but-seemingly-correct restructuring done here rides into the reviewers unchallenged, where a blind rewrite can no longer be caught and reverted.
- **Needs an operator decision** (a scope question only the operator can settle) → strategic halt. Stop the chain; under autopilot append `{"event": "self_review_halt", "reason": "<the question>"}` to `$RUN_LOG`. Do not guess the operator's intent.

**Autopilot dry-run (`GOODFELLOW_AUTOPILOT=dry-run`):** don't apply self-review fixes inline. For each small-unambiguous fix you would make, log `{"event": "self_review_fix", "would_act": true, "fix": "<one-line>"}` to `$RUN_LOG` instead of editing. (Large/ambiguous findings carry to the reviewer rounds as usual; halts log as above. This branch scopes only the step-0.5 edits — the research-injection append has its own dry-run branch in step 1.)

## 1. Research injection (before adversarial rounds)

Extract factual, externally verifiable claims from the plan (this is cheap and reads the plan already in context):
- Library/framework version claims
- API endpoint behavior
- Tool availability and flags
- Rate limits, quotas, TTLs

Announce: "Researching N claims: [summary]."

**Run the research in a dedicated subagent** (vanilla Task tool, one single subagent — do NOT fan out per-claim; the batch script already parallelizes internally). The win is **parent context hygiene**: the subagent absorbs the raw web snippets / search output and the parent only ever sees the compact appendix, keeping the reviewing model's window free of noise. Dispatch prompt:

> "Run `bash \"${CLAUDE_PLUGIN_ROOT}/scripts/research.sh\" --claims '<json array of claims>' --max 5` (Tavily batch if `GOODFELLOW_TAVILY_KEY` is set, else WebSearch fallback). For each ✓ (relevance-matched) claim, open the cited source and confirm whether it actually supports the claim. Return ONLY the compact appendix below — no raw search output, no snippets, no commentary. If research tooling is unavailable, return exactly `RESEARCH_SKIPPED: <reason>`.
>
> ```
> ## Appendix: Researched Claims (research pass YYYY-MM-DD)
>
> ✓ Claim: <text>. Supporting source: <source URL> (relevance match — not adjudicated).
> ✗ Claim: <text>. Cited source read and it contradicts the claim: <source URL>.
> ? Claim: <text>. No clear source — flagged for reviewers.
> ```"

The parent appends the returned appendix verbatim to the plan (or, on `RESEARCH_SKIPPED`, logs the reason and proceeds).

Note: the Tavily adapter uses a word-overlap heuristic — ✓ means *a relevant source was found*, NOT that the claim was confirmed. It scores relevance only, so a contradicting source scores the same as a confirming one and there is no refutation signal in the adapter itself (a ✗ line only appears when the research subagent manually read a ✓ source and found it contradictory). Proceed to adversarial after appending the appendix.

**Autopilot dry-run (`GOODFELLOW_AUTOPILOT=dry-run`):** do NOT append the appendix to the plan file. The subagent still returns the appendix, but the parent logs `{"event": "would_append_verified_claims", "would_act": true, "claims": <n>, "source_matched": <n>, "no_source": <n>}` to `$RUN_LOG` (from step 0.5) instead of writing it. The dry-run contract is observe-without-mutating; the appendix is a project-file mutation. Proceed to adversarial.

**Graceful degradation:** if the subagent returns `RESEARCH_SKIPPED` (WebSearch/Tavily unavailable), skip. Log reason. Proceed to adversarial.

## 2. Adversarial loop (same structure as spec-review)

Each round, dispatch both reviewers in parallel with **distinct lenses** — reviewer 1 owns the requirements/testability side, reviewer 2 owns the correctness side — so the parallel slot buys coverage diversity, not duplicate hunting. Both emit the **identical output format** (`## Verdict / ## Blockers / ## Major / ## Minor`, per-finding: cite section, explain issue, state fix); only the focus differs.

**Reviewer 1 (Claude subagent, model: sonnet or GOODFELLOW_REVIEW_MODEL) — testability / requirements lens:**

> "You are an adversarial plan reviewer with a **testability, acceptance-criteria, requirements-completeness, and principle-compliance lens**. Read <path>. Focus on: acceptance criteria that can't be objectively tested; spec-coverage gaps (spec requirements with no plan task); tasks whose done-criteria are ambiguous or contradictory; missing tests; and compliance with the seeded design principles (cite violations by P-NNN) plus .goodfellow/knowledge.md principles/gotchas if provided. Challenge '?' claims in the Researched Claims appendix (✓ is relevance only, not a verification verdict). Output: ## Verdict / ## Blockers / ## Major / ## Minor. Per-finding: cite section, explain issue, state fix."

**Reviewer 2 (Codex bridge) — correctness / sequencing lens:**

Use `--file <plan-path>` — a freshly-written plan is usually still untracked and appears in no git diff, so a diff-scoped review would hand the reviewer an EMPTY context. `--file` embeds the actual plan body. Pass the correctness lens as the trailing `-- <prompt>` (the bridge honors it in both the Codex path and the Claude fallback):

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/codex-bridge.sh" --kind plan --file <plan-path> \
  -- "Review this plan with a correctness, edge-cases, hidden-coupling, and contract-integrity lens. Focus on: missing prerequisites, wrong execution order (a task referencing something built later), unaddressed dependencies, steps that will fail at runtime, missing rollback paths, hidden coupling between phases, and risky API/contract assumptions. Challenge '?' claims in the Researched Claims appendix (✓ is relevance only, not a verification verdict). Output: ## Verdict / ## Blockers / ## Major / ## Minor. Per-finding: cite section, explain issue, state fix."
```

If Codex is absent, the bridge falls back to a single Claude reviewer automatically — but because the lens is passed via `-- <prompt>`, that fallback reviewer still runs the correctness lens. So in no-Codex fallback mode (both reviewers are Claude) the two lenses still apply, preserving lens diversity even without model diversity.

**Failed-review contract:** on success the bridge prints an artifact path on stdout; if it exits nonzero it prints `REVIEW_FAILED <code> <class>` instead. Treat that as a FAILED review round, never clean/LGTM — reject the `REVIEW_FAILED` prefix before any read, surface it, and stop the round rather than counting an empty result as convergence.

## 3. Reconcile + address (no gate rounds 1-3)

Same flow as spec-review: deduplicate, present, fix blockers + majors, re-dispatch.

## 4. Verifier pass (round 2+)

Before fixing, dispatch a **single batched verifier** subagent that receives **all** of the round's findings at once (not one subagent per finding — the batched call mirrors the `build_verifier_input(findings, …)` pattern: one dispatch in, per-finding verdicts out). Give it the numbered findings list and the current plan; it returns a `real` / `stale` / `noise` verdict per finding id. Only `real` findings proceed to fix.

## 5. Convergence

Same rules as spec-review. Hard cap 6. Deferred findings: discard (plan-review doesn't file loops).

## 6. After convergence

Summarize: "Plan converged at round N. Research found supporting sources for X/Y claims (relevance-matched, not adjudicated), Z with no clear source." (The Tavily adapter has no refutation path and ✓ is relevance only — never report a "verified" or "refuted" count.)

Auto-dispatch `/goodfellow:execute <plan-path>`.
