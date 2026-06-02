#!/usr/bin/env bash
# Goodfellow Codex bridge — wraps codex exec review or falls back to dual-Claude.
# Usage: codex-bridge.sh --kind <spec|plan|diff> [--model <sonnet|opus|haiku>]
#        [--include-aesthetic] [--commit <sha>] [--base <branch>] [--uncommitted]
#        [-- <prompt>]
set -euo pipefail

KIND=""
MODEL="${GOODFELLOW_REVIEW_MODEL:-sonnet}"
INCLUDE_AESTHETIC=""
COMMIT=""
BASE=""
UNCOMMITTED=""
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
    --) shift; PROMPT="$*"; break ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

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
  if [[ -n "$COMMIT" ]]; then
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
  [[ -n "$MODEL" ]] && args+=(--model "$MODEL")

  local review_prompt
  review_prompt=$(build_review_prompt)

  # codex exec review: scope flags reject positional PROMPT — pipe via stdin
  local rc=0
  if [[ -n "$COMMIT" || -n "$BASE" || -n "$UNCOMMITTED" ]]; then
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

  local full_adversarial full_constructive
  full_adversarial="You are an adversarial $KIND reviewer.

$review_prompt

$(if [[ -n "$context" ]]; then echo "Here is the content to review:"; echo '```'; echo "$context"; echo '```'; fi)

Output: ## Verdict / ## Blockers / ## Major / ## Minor. Per-finding: cite section, explain issue, state fix."

  full_constructive="You are a constructive $KIND reviewer.

$review_prompt

$(if [[ -n "$context" ]]; then echo "Here is the content to review:"; echo '```'; echo "$context"; echo '```'; fi)

Output: ## Strengths / ## Gaps / ## Suggestions."

  local fail_count=0

  {
    echo "--- REVIEWER_1 (adversarial, model: $MODEL) ---"
    if command -v claude &>/dev/null; then
      echo "$full_adversarial" | timeout 300 claude --print --model "$MODEL" 2>/dev/null || { echo "(adversarial reviewer failed)"; fail_count=$((fail_count + 1)); }
    else
      echo "(claude CLI not available — adversarial prompt below)"
      echo "$full_adversarial"
      fail_count=$((fail_count + 1))
    fi
    echo ""
    echo "--- REVIEWER_2 (constructive, model: $MODEL) ---"
    if command -v claude &>/dev/null; then
      echo "$full_constructive" | timeout 300 claude --print --model "$MODEL" 2>/dev/null || { echo "(constructive reviewer failed)"; fail_count=$((fail_count + 1)); }
    else
      echo "(claude CLI not available — constructive prompt below)"
      echo "$full_constructive"
      fail_count=$((fail_count + 1))
    fi
  } > "$OUTFILE"

  if [[ $fail_count -ge 2 ]]; then
    echo "ERROR: Both Claude fallback reviewers failed" >&2
    exit 1
  fi
  if [[ $fail_count -ge 1 ]]; then
    echo "WARNING: One Claude fallback reviewer failed (partial review)" >&2
  fi
}

if has_codex; then
  run_codex
else
  run_claude_fallback
fi

echo "$OUTFILE"
