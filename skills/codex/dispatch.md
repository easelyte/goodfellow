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

