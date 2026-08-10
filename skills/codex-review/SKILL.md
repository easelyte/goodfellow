---
name: codex-review
description: Direct Codex adversarial review on the current diff, a specific file, or a commit. Two-stage generator+judge pipeline; falls back to a single Claude reviewer when Codex is unavailable.
---

Run a Codex adversarial review.

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

## 1. Determine review target

Based on operator's prompt:
- No args or `--uncommitted` → review uncommitted changes
- `--commit <sha>` → review a specific commit
- `--base <branch>` → review changes against a base branch

## 2. Dispatch review

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/codex-bridge.sh" --kind diff --uncommitted
```

Or with specific flags:
```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/codex-bridge.sh" --kind diff --commit <sha>
bash "${CLAUDE_PLUGIN_ROOT}/scripts/codex-bridge.sh" --kind diff --base <branch>
```

The bridge handles Codex detection and Claude fallback automatically. When
GOODFELLOW_REVIEW_MODEL is set, it's passed through to the Claude fallback
reviewer only (never to Codex).

**Output contract — check the exit code before reading the path.** On success the
LAST stdout line is a readable review-artifact path. On failure the bridge exits
nonzero AND its last stdout line is `REVIEW_FAILED <rc> <class>` (never a path).
Reject a `REVIEW_FAILED ` prefix before treating the line as a file to read:

```bash
OUT=$(bash "${CLAUDE_PLUGIN_ROOT}/scripts/codex-bridge.sh" --kind diff --uncommitted) || {
  echo "review failed: $OUT" >&2   # $OUT is the REVIEW_FAILED sentinel line
  exit 1
}
case "$OUT" in REVIEW_FAILED\ *) echo "review failed: $OUT" >&2; exit 1 ;; esac
# safe to read "$OUT" now
```

**Failed-review contract:** on success the bridge prints an artifact path on stdout; if it exits nonzero it prints `REVIEW_FAILED <code> <class>` instead of a path. Treat that as a FAILED review, never as clean/LGTM — reject the `REVIEW_FAILED` prefix before reading anything, surface it, and stop. Never report "no findings" from a nonzero run.

## 3. Present findings

Read the review output. Present:
- Blockers (if any)
- Major findings
- Minor findings
- The `## Judge audit` section (which findings the judge kept/dropped), or a note
  that the review is UNJUDGED (degraded pass or Claude fallback).

No loop filing — codex-review is a standalone tool, not part of the ship flow.
