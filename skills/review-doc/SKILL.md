---
name: review-doc
description: "Multi-round adversarial review of a spec or a plan — research injection, two reviewers with distinct lenses (Claude + Codex, or Claude alone), a verifier pass, and knowledge-file principle checking. Use when someone says \"review my spec\", \"is the spec ready\", \"stress test this spec\", \"review my plan\", \"is the plan ready\", \"stress test this plan\", \"what could go wrong with this plan\" or \"poke holes in this design doc\". `--spec <path>` hands off to plan; `--plan <path>` hands off to execute."
---

Review the document the operator indicated: $ARGUMENTS

## 0. Mode and inputs

- **Mode.** `--spec <path>` or `--plan <path>`. Without a flag, infer it: a file under
  `docs/plans/` or with a `spec:` key in its frontmatter is a plan; anything else is a spec.
- **Tier.** Read `tier:` from the document's frontmatter (written by `brainstorm`). A spec or plan
  exists only at T2 and T3. At T3, a plan must contain a **rehearsal task**: perform the real
  mutations against a sandbox the user supplies (a database copy, a temporary tree, a namespace)
  and record the evidence for the PR. A T3 plan without one is a blocker.

Read the document fully (and, for a plan, its spec from the `spec:` frontmatter key). Then read the
project's accumulated knowledge, backend-aware (invalid `GOODFELLOW_MEMORY` hard-errors here):

```bash
MODE=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory_config.py" resolve-mode) || { echo "$MODE"; exit 1; }
if [ "$MODE" = "rich" ]; then
  # Full MEMORY.md index (incl. ## Pending (unconfirmed) — discount those). Internally
  # falls back: .migrating -> knowledge.md (no regen), absent -> knowledge.md, dirty/stale -> regenerate.
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory_index.py" --root .goodfellow read-index
else
  cat .goodfellow/knowledge.md 2>/dev/null || true   # flat: Principles + Gotchas inform principle checking
fi
```

In rich mode, auto-pull bodies of exact-`domain` matches; open other relevant fact bodies by name from the index.

Also read the plugin-shipped universal design principles and flag violations by their `P-NNN` id (the web supplement is read only when web context is opted in — `GOODFELLOW_PRINCIPLES_WEB=1` or a `package.json` at the project root; an invalid value hard-errors here):

```bash
# Progressive disclosure (docs/instruction-density-budget.md): inject only the
# principle INDEX (P-NNN id + title + one-line rule), NOT the full bodies. The full
# corpus is ~17k tokens / ~300 directives — well past the accuracy-erosion ceiling.
# All error handling stays in Python (bad config / missing core / unreadable file ->
# non-zero exit + stderr); a non-zero exit means a config/packaging problem — stop.
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/principles_context.py" --index --project-root .
```

Scan the category routing table. For any category relevant to this work, expand its one-liners with
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/principles_context.py" --category NAME --project-root .`, then pull the
full body of a relevant principle with `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/principles_context.py" --show P-NNN [P-NNN ...] --project-root .`
before applying or citing it — requesting a parent id (e.g. `P-017`) includes its
sub-principles. Cite violations by P-NNN.

Initialize the run log so every decision below has a concrete destination:

```bash
RUN_LOG=$(bash "${CLAUDE_PLUGIN_ROOT}/scripts/run_log.sh")
```

Use `$RUN_LOG` for every append in this skill, never a literal `<timestamp>.jsonl` placeholder.

## 1. Self-review pass

Read it once as its author would: internal contradictions, undefined behaviour at decision
boundaries, untestable success criteria, knowledge-gotcha violations; for a plan also missing
dependencies, wrong execution order, spec sections with no task, and high-stakes tasks with no
expected red.

- **Small and unambiguous** (typo, dangling reference, one-line clarification): fix inline now.
- **Large or ambiguous** (a restructuring, a contradiction whose right resolution is unclear): do
  NOT fix here. Leave it for the reviewers, who can then catch a wrong fix.
- **A product call** (naming, pricing, public positioning, taste-only UX, scope beyond the brief):
  stop and ask; under autopilot append `{"event": "self_review_halt", "reason": "<question>"}` to
  `$RUN_LOG`. Do not guess.

**Dry-run (`GOODFELLOW_AUTOPILOT=dry-run`):** log `{"event": "self_review_fix", "would_act": true, "fix": "<one-line>"}` for each fix instead of editing.

## 2. Research injection

Extract the load-bearing factual claims (library and API behaviour, versions, tool flags, limits).
**Spec mode:** after round 1, from the claims the findings depend on. **Plan mode:** before round 1,
from the plan itself. Run it in one dedicated subagent so raw search output stays out of your
context:

> "Write the load-bearing claims as a JSON array of strings to a temp file with the Write tool (never put claim text on the command line), then run `bash \"${CLAUDE_PLUGIN_ROOT}/scripts/research.sh\" --claims-file <that file> --max 5` to prepare the claim list, then verify each claim via WebSearch. For each claim, open the cited source and confirm whether it supports the claim. Return ONLY this appendix, or exactly `RESEARCH_SKIPPED: <reason>`:
>
> ```
> ## Appendix: Researched Claims (research pass YYYY-MM-DD)
> ✓ Claim: <text>. Supporting source read: <URL>.
> ✗ Claim: <text>. Cited source read and it contradicts the claim: <URL>.
> ? Claim: <text>. No clear source — flagged for reviewers.
> ```"

Append the appendix to the document. ✓ means a cited source was read and supports the claim, ✗ that
it contradicts, ? that no clear source was found. On `RESEARCH_SKIPPED`, log the reason and continue;
findings keep their severity.

**Dry-run:** do not append; log `{"event": "would_append_verified_claims", "would_act": true, "claims": <n>, "source_matched": <n>, "no_source": <n>}`.

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

## 5. Convergence

Converged when new findings drop to polish-tier. **Hard cap: 6 rounds**, a terminal state
`resolved | limit_reached` (P-079). Reaching the cap is a limit, never convergence:

- Safety-critical findings remain → `limit_reached`: halt, recommend a rewrite.
- Only non-blocking findings → `limit_reached`: stop and report the deferred findings as a limit,
  NOT as convergence.
- No findings → `resolved`.

At `resolved`, promote `confidence:` in the frontmatter if review settled the open architectural
questions. If the frontmatter carries pending-review keys (`review_status: pending`,
`failed_reviewers`, `resume`, written by `brainstorm --grill`), remove them now; keep them on any
other outcome so a later session can resume.

## 6. After the loop

Report honestly (P-079): "Spec converged at round N. Key changes: …" or "Plan review halted at
hard cap (round N); deferred: …". Never present a cap-halt as full resolution. Report research as
"supporting sources for X/Y claims (relevance-matched, not adjudicated)", never as "verified".

**Terminal safety gate (P-079).** If §5 ended in a safety-critical cap-halt, STOP. For a spec: do
NOT discard the findings and do NOT auto-dispatch plan; under autopilot append
`{"event": "spec_review_halt", "reason": "safety-critical findings remain at hard cap", "spec": "<path>"}`
to `$RUN_LOG`. For a plan: do NOT discard the findings and do NOT auto-dispatch execute; under
autopilot append `{"event": "plan_review_halt", "reason": "safety-critical findings remain at hard cap", "plan": "<path>"}`.
Surface the findings and recommend a rewrite. A known-unsafe document must not advance.

Otherwise (genuine convergence, or a cap-halt with only non-blocking findings), carry the residue
forward so nothing silently disappears: file each remaining **major** as a loop
(`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/loop_store.py" --root . add "<title>" --priority p3 --source "review-doc-r<N>" --description "<finding>"`)
and list it in the hand-off summary; remaining minors are dropped. (Dry-run: log each as a
`would_file_loop` event instead.)
On a safety-critical cap-halt, do NOT discard them; the gate above preserves and surfaces them.

**Halt key.** If the frontmatter says `next_action: halt-after-spec-review`, stop after the spec
review and say: "Spec converged at round N. Frontmatter requests `halt-after-spec-review`; run
`/goodfellow:plan <path>` when ready." Under autopilot also append
`{"event": "halt_after_spec_review", "spec": "<path>"}`.

**Hand-off.** Spec mode: dispatch `/goodfellow:plan <spec-path>`. Plan mode: dispatch
`/goodfellow:execute <plan-path>`.
