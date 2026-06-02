#!/usr/bin/env bash
# Ensure .shipline/ exists and is gitignored in the user's project.
set -euo pipefail

PROJECT_ROOT="${1:-.}"

mkdir -p "$PROJECT_ROOT/.shipline"

GITIGNORE="$PROJECT_ROOT/.gitignore"
if [ -f "$GITIGNORE" ]; then
  grep -qxF '.shipline/' "$GITIGNORE" || echo '.shipline/' >> "$GITIGNORE"
else
  echo '.shipline/' > "$GITIGNORE"
fi
