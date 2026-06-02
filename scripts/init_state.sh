#!/usr/bin/env bash
# Ensure .shipline/ exists and is gitignored in the user's project.
set -euo pipefail

PROJECT_ROOT="${1:-.}"

mkdir -p "$PROJECT_ROOT/.shipline"

GITIGNORE="$PROJECT_ROOT/.gitignore"
if [ -f "$GITIGNORE" ]; then
  if ! grep -qxF '.shipline/' "$GITIGNORE"; then
    # Ensure trailing newline before appending
    [[ -s "$GITIGNORE" && "$(tail -c1 "$GITIGNORE")" != "" ]] && echo "" >> "$GITIGNORE"
    echo '.shipline/' >> "$GITIGNORE"
  fi
else
  echo '.shipline/' > "$GITIGNORE"
fi
