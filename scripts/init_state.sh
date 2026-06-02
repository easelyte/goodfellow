#!/usr/bin/env bash
# Ensure .goodfellow/ exists and is gitignored in the user's project.
set -euo pipefail

PROJECT_ROOT="${1:-.}"

mkdir -p "$PROJECT_ROOT/.goodfellow/runs"

GITIGNORE="$PROJECT_ROOT/.gitignore"
if [ -f "$GITIGNORE" ]; then
  if ! grep -qxF '.goodfellow/' "$GITIGNORE"; then
    # Ensure trailing newline before appending
    [[ -s "$GITIGNORE" && "$(tail -c1 "$GITIGNORE")" != "" ]] && echo "" >> "$GITIGNORE"
    echo '.goodfellow/' >> "$GITIGNORE"
  fi
else
  echo '.goodfellow/' > "$GITIGNORE"
fi
