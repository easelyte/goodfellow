#!/usr/bin/env bash
# Shipline Codex bridge — wraps codex exec review or falls back to dual-Claude.
# Usage: codex-bridge.sh --kind <spec|plan|diff> [--model <sonnet|opus|haiku>]
#        [--include-aesthetic] [--commit <sha>] [--base <branch>] [--uncommitted]
#        [-- <prompt>]
set -euo pipefail

KIND=""
MODEL="${SHIPLINE_REVIEW_MODEL:-sonnet}"
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

OUTFILE=$(mktemp /tmp/shipline-review-XXXXXX.md)

has_codex() {
  [[ "${SHIPLINE_CODEX:-1}" != "0" ]] && command -v codex &>/dev/null
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

run_codex() {
  check_version

  local args=(codex exec review)
  [[ -n "$COMMIT" ]] && args+=(--commit "$COMMIT")
  [[ -n "$BASE" ]] && args+=(--base "$BASE")
  [[ -n "$UNCOMMITTED" ]] && args+=(--uncommitted)

  local review_prompt="Review this $KIND. Focus on contradictions, undefined behavior, missing requirements, ambiguous criteria."
  [[ -n "$INCLUDE_AESTHETIC" ]] && review_prompt="$review_prompt Include aesthetic/style findings."
  [[ -n "$PROMPT" ]] && review_prompt="$PROMPT"

  local rc=0
  timeout 300 "${args[@]}" "$review_prompt" > "$OUTFILE" 2>&1 || rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "ERROR: Codex review timed out or failed (exit $rc)" >&2
    exit 1
  fi
}

run_claude_fallback() {
  local adversarial_prompt constructive_prompt

  adversarial_prompt="You are an adversarial $KIND reviewer. Find weaknesses: contradictions, undefined behavior, missing requirements, ambiguous success criteria, hidden coupling. Output: ## Verdict / ## Blockers / ## Major / ## Minor. Per-finding: cite section, explain issue, state fix."
  constructive_prompt="You are a constructive $KIND reviewer. Verify the design works: check completeness, identify what's well-specified, flag gaps where implementation will need judgment calls. Output: ## Strengths / ## Gaps / ## Suggestions."

  [[ -n "$INCLUDE_AESTHETIC" ]] && {
    adversarial_prompt="$adversarial_prompt Also include style and consistency findings."
    constructive_prompt="$constructive_prompt Also note style inconsistencies."
  }

  {
    echo "--- REVIEWER_1 (adversarial, model: $MODEL) ---"
    if command -v claude &>/dev/null; then
      echo "$adversarial_prompt" | timeout 300 claude --print --model "$MODEL" 2>/dev/null || echo "(adversarial reviewer failed)"
    else
      echo "(claude CLI not available — adversarial prompt below)"
      echo "$adversarial_prompt"
    fi
    echo ""
    echo "--- REVIEWER_2 (constructive, model: sonnet) ---"
    if command -v claude &>/dev/null; then
      echo "$constructive_prompt" | timeout 300 claude --print --model sonnet 2>/dev/null || echo "(constructive reviewer failed)"
    else
      echo "(claude CLI not available — constructive prompt below)"
      echo "$constructive_prompt"
    fi
  } > "$OUTFILE"
}

if has_codex; then
  run_codex
else
  run_claude_fallback
fi

echo "$OUTFILE"
