#!/usr/bin/env bash
# Goodfellow research adapter — batch-verify factual claims via Tavily or WebSearch fallback.
# Usage: research.sh --claims '<json array of claim strings>' [--max <N>]
# Requires: GOODFELLOW_TAVILY_KEY for Tavily (optional — falls back to printing claims for WebSearch)
set -euo pipefail

CLAIMS=""
MAX_SEARCHES=5

while [[ $# -gt 0 ]]; do
  case "$1" in
    --claims) CLAIMS="$2"; shift 2 ;;
    --max) MAX_SEARCHES="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$CLAIMS" ]]; then
  echo "ERROR: --claims required (JSON array of strings)" >&2
  exit 1
fi

OUTFILE=$(mktemp /tmp/goodfellow-research-XXXXXX)

if [[ -n "${GOODFELLOW_TAVILY_KEY:-}" ]]; then
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/research_tavily.py" \
    --claims "$CLAIMS" \
    --max "$MAX_SEARCHES" \
    --api-key "$GOODFELLOW_TAVILY_KEY" \
    > "$OUTFILE"
else
  echo "## Research: Tavily not configured (set GOODFELLOW_TAVILY_KEY)" > "$OUTFILE"
  echo "" >> "$OUTFILE"
  echo "Claims to verify via WebSearch:" >> "$OUTFILE"
  echo "$CLAIMS" | python3 -c "
import json, sys
claims = json.load(sys.stdin)
for i, c in enumerate(claims[:${MAX_SEARCHES}], 1):
    print(f'{i}. {c}')
" >> "$OUTFILE"
  echo "" >> "$OUTFILE"
  echo "Falling back to WebSearch — dispatch searches manually from the skill." >> "$OUTFILE"
fi

echo "$OUTFILE"
