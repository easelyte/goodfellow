#!/usr/bin/env bash
# Goodfellow Codex bridge — wraps codex exec review or falls back to dual-Claude.
# Usage: codex-bridge.sh --kind <spec|plan|diff> [--model <sonnet|opus|haiku>]
#        [--include-aesthetic] [--commit <sha>] [--base <branch>] [--uncommitted]
#        [--file <path>] [-- <prompt>]
#
# MODEL ($GOODFELLOW_REVIEW_MODEL, default sonnet) is a CLAUDE model id and is
# only ever passed to the Claude fallback reviewer. The Codex path must NOT
# receive a Claude model name (codex exec expects a GPT model id); it stays on
# codex's configured default unless $GOODFELLOW_CODEX_MODEL is set to a GPT id.
#
# --file <path> reviews a file's CONTENT directly (for freshly-written, still
# untracked spec/plan artifacts that appear in no git diff). It embeds the file
# body into the review context rather than relying on git show/diff.
set -euo pipefail

KIND=""
MODEL="${GOODFELLOW_REVIEW_MODEL:-sonnet}"
# GPT model id for the Codex path only. Empty => codex uses its configured default.
CODEX_MODEL="${GOODFELLOW_CODEX_MODEL:-}"
INCLUDE_AESTHETIC=""
COMMIT=""
BASE=""
UNCOMMITTED=""
FILE=""
PROMPT=""
MIN_VERSION="0.120.0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --kind) KIND="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --include-aesthetic) INCLUDE_AESTHETIC="1"; shift ;;
    --commit) COMMIT="$2"; shift 2 ;;
    --base) BASE="$2"; shift 2 ;;
    --uncommitted) UNCOMMITTED="1"; shift ;;
    --file) FILE="$2"; shift 2 ;;
    --) shift; PROMPT="$*"; break ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [[ -n "$FILE" && ! -f "$FILE" ]]; then
  echo "ERROR: --file target not found: $FILE" >&2
  exit 1
fi

OUTFILE=$(mktemp /tmp/goodfellow-review-XXXXXX)

has_codex() {
  [[ "${GOODFELLOW_CODEX:-1}" != "0" ]] && command -v codex &>/dev/null
}

check_version() {
  local ver
  ver=$(codex --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
  if [[ -z "$ver" ]]; then
    echo "WARNING: Could not detect Codex version" >&2
    return
  fi
  local lowest
  lowest=$(printf '%s\n%s\n' "$MIN_VERSION" "$ver" | sort -V | head -1)
  if [[ "$lowest" == "$ver" && "$ver" != "$MIN_VERSION" ]]; then
    echo "WARNING: Codex $ver < minimum $MIN_VERSION — output may be degraded" >&2
  fi
}

# Collect the diff/content that the review should cover
get_review_context() {
  if [[ -n "$FILE" ]]; then
    # Freshly-written spec/plan artifacts are untracked and appear in no git
    # diff — feed the reviewer the actual file body.
    cat "$FILE" 2>/dev/null || echo "(could not read file $FILE)"
  elif [[ -n "$COMMIT" ]]; then
    git show "$COMMIT" 2>/dev/null || echo "(could not read commit $COMMIT)"
  elif [[ -n "$BASE" ]]; then
    git diff "$BASE"...HEAD 2>/dev/null || echo "(could not diff against $BASE)"
  elif [[ -n "$UNCOMMITTED" ]]; then
    git diff HEAD 2>/dev/null; git diff --cached 2>/dev/null
  fi
}

build_review_prompt() {
  local base_prompt="Review this $KIND. Focus on contradictions, undefined behavior, missing requirements, ambiguous criteria."
  [[ -n "$INCLUDE_AESTHETIC" ]] && base_prompt="$base_prompt Include aesthetic/style findings."
  [[ -n "$PROMPT" ]] && base_prompt="$PROMPT"
  echo "$base_prompt"
}

run_codex() {
  check_version

  local args=(codex exec review)
  [[ -n "$COMMIT" ]] && args+=(--commit "$COMMIT")
  [[ -n "$BASE" ]] && args+=(--base "$BASE")
  [[ -n "$UNCOMMITTED" ]] && args+=(--uncommitted)
  # Codex/GPT model id ONLY — never $MODEL (a Claude id). Empty => codex default.
  [[ -n "$CODEX_MODEL" ]] && args+=(--model "$CODEX_MODEL")

  local review_prompt
  review_prompt=$(build_review_prompt)

  local rc=0
  if [[ -n "$FILE" ]]; then
    # --file mode has no codex scope flag; embed the file body into the prompt
    # (positional) so an untracked artifact is actually reviewed.
    local context full_prompt
    context=$(get_review_context)
    full_prompt="$review_prompt

Here is the $KIND to review (file: $FILE):
\`\`\`
$context
\`\`\`"
    timeout 300 "${args[@]}" "$full_prompt" > "$OUTFILE" 2>&1 || rc=$?
  elif [[ -n "$COMMIT" || -n "$BASE" || -n "$UNCOMMITTED" ]]; then
    # codex exec review: scope flags reject positional PROMPT — pipe via stdin
    echo "$review_prompt" | timeout 300 "${args[@]}" - > "$OUTFILE" 2>&1 || rc=$?
  else
    timeout 300 "${args[@]}" "$review_prompt" > "$OUTFILE" 2>&1 || rc=$?
  fi
  if [[ $rc -ne 0 ]]; then
    echo "ERROR: Codex review timed out or failed (exit $rc)" >&2
    exit 1
  fi
}

run_claude_fallback() {
  local review_prompt context
  review_prompt=$(build_review_prompt)
  context=$(get_review_context)

  local full_prompt
  full_prompt="You are an adversarial $KIND reviewer.

$review_prompt

$(if [[ -n "$context" ]]; then echo "Here is the content to review:"; echo '```'; echo "$context"; echo '```'; fi)

Output: ## Verdict / ## Blockers / ## Major / ## Minor. Per-finding: cite section, explain issue, state fix."

  {
    echo "--- REVIEWER (single-model fallback, model: $MODEL) ---"
    if command -v claude &>/dev/null; then
      echo "$full_prompt" | timeout 300 claude --print --model "$MODEL" 2>/dev/null || {
        echo "ERROR: Claude fallback reviewer failed" >&2
        exit 1
      }
    else
      echo "ERROR: Neither Codex nor Claude CLI available" >&2
      exit 1
    fi
  } > "$OUTFILE"
}

if has_codex; then
  run_codex
else
  run_claude_fallback
fi

echo "$OUTFILE"
