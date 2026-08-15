---
name: spec-review
description: Multi-round adversarial spec review with research injection, verifier pass, and knowledge-file principle checking. Dispatches two reviewers per round (Claude + Codex, or single Claude fallback).
---

Run a multi-round adversarial review on the spec file the operator indicated.

## 0. Read the spec

Read the spec file fully, then read the project's accumulated knowledge, backend-aware (invalid `GOODFELLOW_MEMORY` hard-errors here):

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

The two reviewers run **distinct lenses** so the parallel slot buys coverage diversity, not duplicate hunting. Reviewer 1 owns the requirements side; reviewer 2 owns the correctness side. Both emit the **identical output format** (`## Verdict / ## Blockers / ## Major / ## Minor`, per-finding: cite section, explain issue, state fix) — only the focus differs.

**Reviewer 1 (Claude subagent, model from GOODFELLOW_REVIEW_MODEL or default sonnet) — testability / requirements lens:**

Use the Agent tool with `model: "sonnet"` (or the value of GOODFELLOW_REVIEW_MODEL). Prompt:

> "You are an adversarial spec reviewer with a **testability, acceptance-criteria, requirements-completeness, and principle-compliance lens**. Read <path>. Focus on: success/acceptance criteria that can't be objectively tested; requirements that are incomplete, ambiguous, or missing entirely; undefined behavior at decision boundaries; and compliance with the seeded design principles (cite violations by P-NNN) plus .goodfellow/knowledge.md principles and gotchas if provided. Output: ## Verdict / ## Blockers / ## Major / ## Minor. Per-finding: cite section, explain issue, state fix. If a finding matches a knowledge gotcha, note 'knowledge-elevated' and bump severity one tier (cap at blocker)."

**Reviewer 2 (Codex bridge) — correctness / security lens:**

Use `--file <spec-path>` — a freshly-written spec is usually still untracked and appears in no git diff, so a diff-scoped review (`--uncommitted`/`--commit`/`--base`) would hand the reviewer an EMPTY context. `--file` embeds the actual spec body. Pass the correctness lens as the trailing `-- <prompt>` (the bridge honors it in both the Codex path and the Claude fallback):

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/codex-bridge.sh" --kind spec --file <spec-path> \
  -- "Review this spec with a correctness, security, edge-cases, hidden-coupling, and contract-integrity lens. Focus on: logical/correctness errors, security exposure, unhandled edge cases and failure modes, hidden coupling between components, and contract integrity (inputs/outputs/invariants other components depend on). Output: ## Verdict / ## Blockers / ## Major / ## Minor. Per-finding: cite section, explain issue, state fix."
```

If Codex is absent, the bridge falls back to a single Claude reviewer automatically — but because the lens is passed via `-- <prompt>`, that fallback reviewer still runs the correctness lens. So in no-Codex fallback mode (both reviewers are Claude) the two lenses still apply, preserving lens diversity even without model diversity.

**Failed-review contract:** on success the bridge prints an artifact path on stdout; if it exits nonzero it prints `REVIEW_FAILED <code> <class>` instead. Treat that as a FAILED review round, never clean/LGTM — reject the `REVIEW_FAILED` prefix before any read, surface it, and stop the round rather than counting an empty result as convergence.

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

Before fixing round 2+ findings, dispatch a **single batched verifier** subagent that receives **all** of the round's findings at once (not one subagent per finding — the batched call mirrors the `build_verifier_input(findings, …)` pattern: one dispatch in, per-finding verdicts out).

Give the verifier the full findings list (numbered) and the current spec, and require a verdict per finding: `real` / `stale` / `noise`. It returns one verdict line per finding id.

Only `real` findings proceed to fix. Stale/noise get noted but not fixed.

## 5. Convergence

Declare convergence when new findings drop from safety-critical to polish-tier.

**Hard cap:** 6 rounds. The cap is a terminal state, `resolved | limit_reached` — reaching it
is a limit, never convergence (P-079). At hard cap:
- Safety-critical findings remain → `limit_reached`: halt, recommend rewrite
- Only non-blocking findings → `limit_reached`: stop the loop, note deferred findings; §6
  reports this as a limit, NOT convergence
- No findings → `resolved` (genuine convergence)

**Confidence promotion:** if spec-review resolves all architecture-changing unresolved_questions, update `confidence:` in spec frontmatter from `low` to `medium` or `high`.

## 6. After convergence

Summarize honestly (P-079 — reaching a limit is not success): if convergence was genuine
(findings resolved to polish-tier or none), "Spec converged at round N. Key changes:
{bullets}." If round N was the hard cap with findings still deferred, report the limit as a
limit instead — "Spec review halted at hard cap (round N); deferred: {bullets}" — never
present a cap-halt as full resolution.

**Terminal safety gate (P-079).** If §5 ended in a safety-critical cap-halt — round 6
reached with unresolved safety-critical findings — STOP here. A cap-halt is a limit-halt,
not convergence, so this step's cascade does not apply: do NOT discard the findings and do
NOT auto-dispatch plan. Surface the unresolved safety-critical findings to the operator and
recommend a spec rewrite. Under autopilot, append `{"event": "spec_review_halt", "reason":
"safety-critical findings remain at hard cap", "spec": "<spec-path>"}` to `$RUN_LOG` and
stop the chain. A known-unsafe spec must not advance to plan.

Otherwise (genuine convergence, or a cap-halt with only non-blocking findings), continue:

Deferred findings: discard (spec-review doesn't file loops — unresolved non-blocking findings are addressed in the next chain stage).

**Halt gate — check before cascading.** Re-read the reviewed spec's frontmatter. If it declares `next_action: halt-after-spec-review`, do NOT auto-dispatch plan. HALT and surface to the operator instead:

> "Spec converged at round N. Frontmatter requests `halt-after-spec-review` — stopping here. Run `/goodfellow:plan <spec-path>` when ready to continue."

Under autopilot, append `{"event": "halt_after_spec_review", "spec": "<spec-path>"}` to `$RUN_LOG` and stop the chain. This matches the autopilot dry-run intent (observe, don't cascade) and lets a spec opt out of the automatic spec→plan handoff.

Otherwise (no `halt-after-spec-review`), auto-dispatch `/goodfellow:plan <spec-path>`.
