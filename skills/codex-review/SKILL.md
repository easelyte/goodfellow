---
name: codex-review
description: Direct Codex adversarial review on the current diff, a specific file, or a commit. Falls back to dual-Claude when Codex is unavailable.
---

Run a Codex adversarial review.

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

The bridge handles Codex detection and Claude fallback automatically. When SHIPLINE_REVIEW_MODEL is set, it's passed through to the Claude fallback reviewer.

## 3. Present findings

Read the review output. Present:
- Blockers (if any)
- Major findings
- Minor findings

No loop filing — codex-review is a standalone tool, not part of the ship flow.
