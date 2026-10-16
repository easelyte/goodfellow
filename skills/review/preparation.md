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

