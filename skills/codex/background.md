## Why Codex as the adversarial reviewer

A second, *independent* model reviewing the change is worth more than one model
reviewing its own work — it catches the failures the author (and the author's
model) rationalized away. The reason to make that second model Codex is **cost**:

- **Near-zero marginal cost.** Run direct via the Codex API, per-token pricing is
  low; run it through a Codex CLI subscription, the included limits are generous
  enough that an adversarial pass on every change is effectively free at the
  margin. Either way you are not paying premium-model rates to review every diff.
- **Second independent model.** Codex is a different model family from the one
  writing the code, so its blind spots differ — the whole point of an adversarial
  second opinion.
- **The result:** an adversarial review you can afford to run on *every* change,
  not just the risky ones — plus a second grounding "judge" pass (below) that
  drops the reviewer's own weak findings, at the same near-zero marginal cost.

## How it works (two-stage generator + judge)

1. **Generator** — an adversarial reviewer inlines the diff/commit/file and emits
   structured per-finding blocks.
2. **Judge** — a second Codex pass grounds-or-drops each finding (evidence +
   confidence threshold + diff-boundary scope). A real blocker is never silently
   dropped: any judge/validation problem fails OPEN to the unjudged findings plus
   a degradation banner. The final review carries a `## Judge audit` table.

When Codex is unavailable the bridge falls back to a single Claude reviewer; that
fallback path is UNJUDGED (no second grounding pass) and says so in its output.

