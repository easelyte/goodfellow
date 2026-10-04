#!/usr/bin/env bash
# Goodfellow research adapter — prepare factual claims for WebSearch verification.
# Usage: research.sh --claims '<json array of claim strings>' [--max <N>]
# Emits a temp-file path holding the capped claim list; the skill dispatches one
# WebSearch per claim. Reads no credentials and makes no network calls itself.
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

# MAX_SEARCHES is interpolated into an inline Python slice below; reject anything
# that is not a plain positive integer to prevent code injection via --max.
if ! [[ "$MAX_SEARCHES" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: --max must be a positive integer (got: $MAX_SEARCHES)" >&2
  exit 1
fi

OUTFILE=$(mktemp /tmp/goodfellow-research-XXXXXX)

{
  echo "## Research: claims to verify via WebSearch"
  echo ""
  echo "$CLAIMS" | python3 -c "
import json, sys
claims = json.load(sys.stdin)
for i, c in enumerate(claims[:${MAX_SEARCHES}], 1):
    print(f'{i}. {c}')
"
  echo ""
  echo "Dispatch one WebSearch per claim from the skill."
} > "$OUTFILE"

echo "$OUTFILE"
