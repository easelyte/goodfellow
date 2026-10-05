#!/usr/bin/env bash
# Goodfellow research adapter — prepare factual claims for WebSearch verification.
# Usage: research.sh --claims-file PATH [--max <N>]
# PATH holds a JSON array of claim strings. Write it with the Write tool; never
# interpolate claim text into the command line (a claim could carry shell syntax).
# Emits a temp-file path holding the capped claim list; the skill then dispatches
# one WebSearch per claim. Reads no credentials and makes no network calls.
set -euo pipefail

CLAIMS_FILE=""
MAX_SEARCHES=5

while [[ $# -gt 0 ]]; do
  case "$1" in
    --claims-file) CLAIMS_FILE="$2"; shift 2 ;;
    --max) MAX_SEARCHES="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$CLAIMS_FILE" ]]; then
  echo "ERROR: --claims-file required (path to a JSON array of claim strings)" >&2
  exit 1
fi
if [[ ! -f "$CLAIMS_FILE" ]]; then
  echo "ERROR: --claims-file not found: $CLAIMS_FILE" >&2
  exit 1
fi
if ! [[ "$MAX_SEARCHES" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: --max must be a positive integer (got: $MAX_SEARCHES)" >&2
  exit 1
fi

OUTFILE=$(mktemp /tmp/goodfellow-research-XXXXXX)

# Claim text is read from the file by json.load and never crosses a shell or a
# Python-source boundary; --max is passed through the environment, not interpolated.
MAX="$MAX_SEARCHES" python3 - "$CLAIMS_FILE" > "$OUTFILE" <<'PY'
import json, os, sys

with open(sys.argv[1], encoding="utf-8") as f:
    claims = json.load(f)
if not isinstance(claims, list):
    sys.exit("ERROR: claims file must contain a JSON array of strings")

cap = int(os.environ["MAX"])
print("## Research: claims to verify via WebSearch")
print()
for i, claim in enumerate(claims[:cap], 1):
    print(f"{i}. {claim}")
print()
print("Dispatch one WebSearch per claim from the skill.")
PY

echo "$OUTFILE"
