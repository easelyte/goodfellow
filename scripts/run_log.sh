#!/usr/bin/env bash
# Initialize .goodfellow/ (dir + gitignore) and print a concrete timestamped
# JSONL run-log path on stdout.
#
# Skills call this at the start of a pass so any decision log (self_review_halt,
# self_review_fix, would_append_verified_claims, ...) has a concrete file to
# append to — never a literal "<timestamp>" placeholder. The file itself is NOT
# created here (only the dir); it appears on first append, so interactive runs
# that log nothing leave no empty litter.
#
# Usage: RUN_LOG=$(bash "${CLAUDE_PLUGIN_ROOT}/scripts/run_log.sh" [PROJECT_ROOT])
set -euo pipefail

PROJECT_ROOT="${1:-.}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Reuse init_state.sh for dir creation + gitignore (idempotent). Suppress its
# stdout so the run-log path is the only thing we emit.
bash "$SCRIPT_DIR/init_state.sh" "$PROJECT_ROOT" >/dev/null

printf '%s/.goodfellow/runs/%s.jsonl\n' "$PROJECT_ROOT" "$(date -u +%Y%m%dT%H%M%SZ)"
