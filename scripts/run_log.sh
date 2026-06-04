#!/usr/bin/env bash
# Initialize .goodfellow/runs/ and print a concrete, collision-resistant
# timestamped JSONL run-log path on stdout.
#
# Skills call this at the start of a pass so any decision log (self_review_halt,
# self_review_fix, would_append_verified_claims, ...) has a concrete file to
# append to — never a literal "<timestamp>" placeholder. The file itself is NOT
# created here (only the dir); it appears on first append, so interactive runs
# that log nothing leave no empty litter.
#
# Dry-run safety: this only creates the .goodfellow/runs/ log destination, which
# README defines as the dry-run audit trail. It does NOT touch .gitignore (a
# tracked project file) under dry-run — that would violate the dry-run
# observe-without-mutating contract. .gitignore initialization stays in
# init_state.sh, run from brainstorm / explicit non-dry-run setup, and is invoked
# here only when NOT in dry-run.
#
# Usage: RUN_LOG=$(bash "${CLAUDE_PLUGIN_ROOT}/scripts/run_log.sh" [PROJECT_ROOT])
set -euo pipefail

PROJECT_ROOT="${1:-.}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The runs dir is the decision-log destination — safe to create in every mode.
mkdir -p "$PROJECT_ROOT/.goodfellow/runs"

# Ensure .goodfellow/ is gitignored — but never under dry-run, which must not
# mutate tracked project files. init_state.sh is idempotent.
if [ "${GOODFELLOW_AUTOPILOT:-}" != "dry-run" ]; then
  bash "$SCRIPT_DIR/init_state.sh" "$PROJECT_ROOT" >/dev/null
fi

# Append the pid so two runs starting in the same UTC second get distinct files
# (the timestamp alone is second-resolution). Still sortable; prune-stale sweeps
# by mtime so the suffix is irrelevant to retention.
printf '%s/.goodfellow/runs/%s-%s.jsonl\n' \
  "$PROJECT_ROOT" "$(date -u +%Y%m%dT%H%M%SZ)" "$$"
