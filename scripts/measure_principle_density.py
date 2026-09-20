#!/usr/bin/env python3
"""Measure the instruction density of goodfellow's seeded principles.

Quantifies what the chain injects into every run and checks it against
the research-derived budget in docs/instruction-density-budget.md.

Two figures matter:
  - INDEX (always injected on every chain skill run: id + title + one-line rule).
    This is the budgeted number — it competes for the model's finite
    instruction-adherence capacity on every run.
  - FULL CORPUS (--emit; every principle body). Loaded ON DEMAND via --show now,
    so this is advisory: it no longer sits in the always-loaded window. A large
    corpus is fine as long as the INDEX stays under cap.

Token estimate: chars/4 (labeled estimate; no local tokenizer, and the Anthropic
count_tokens endpoint is a metered API call). chars/4 sits between plain English
(~4.7 c/tok) and path/punctuation-dense markdown (~3.5 c/tok); treat +/-15%.

Instruction count: a "standing directive" heuristic. After stripping fenced code,
each bullet / sentence-ish segment counts as one directive if it opens with an
imperative verb OR contains a directive modal (must/never/always/only/do not/...).
Fuzzy by construction (+/-30%); applied identically each run so deltas are comparable.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

import principles_context as pc

# --- research thresholds (see docs/instruction-density-budget.md) ---
TOKEN_EROSION_LO, TOKEN_EROSION_HI = 2500, 3000  # accuracy erodes past this
INSTR_CLIFF_LO, INSTR_CLIFF_HI = 150, 250  # adherence cliff for reasoning models

# --- goodfellow budget ---
# The INDEX is what loads every run, so it is the hard-capped number. Two configs:
#   core-only   — the default always-injected set every user gets. Capped at the
#                 erosion ceiling's top edge (3,000 tok), a little headroom above
#                 today's ~2.8k so a couple more principles fit before displacement.
#   core + web  — opt-in supplement (GOODFELLOW_PRINCIPLES_WEB=1 / a package.json).
#                 The operator chose to spend that extra budget, so it gets a
#                 little more room. Capped ~600 tok higher.
# Past either cap, growth is displacement: a new principle merges into / subsumes an
# existing one (docs/instruction-density-budget.md §B).
INDEX_TOKEN_CAP = 3000
INDEX_ENTRY_CAP = 80
INDEX_TOKEN_CAP_WEB = 3600
INDEX_ENTRY_CAP_WEB = 95

_DIRECTIVE = re.compile(
    r"\b(must|never|always|do not|don't|dont|avoid|ensure|only|prefer|should|"
    r"shall|require[ds]?|stage|keep|drop|read|run|use|no |not\b|blocked?|denied|"
    r"halt|stop)\b",
    re.I,
)
_IMP_VERBS = (
    r"(read|run|use|stage|keep|drop|commit|push|check|verify|never|always|avoid|"
    r"ensure|prefer|do|don't|make|write|add|remove|apply|set|call|open|close|"
    r"report|file|treat|start|leave|cut|reserve|lead|paraphrase|skip|block|halt|"
    r"stop|merge|follow|update|record)"
)
_IMP_START = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?(?:%s)\b" % _IMP_VERBS, re.I)


def _strip_code(t: str) -> str:
    return re.sub(r"```.*?```", " ", t, flags=re.S)


def _segments(t: str):
    for line in t.split("\n"):
        line = line.strip()
        if not line:
            continue
        for part in re.split(r"(?<=[.!?:])\s+(?=[A-Z(*`\-])", line):
            part = part.strip()
            if part:
                yield part


def measure(text: str):
    """Return (chars, est_tokens, instruction_count)."""
    text = _strip_code(text)
    chars = len(text)
    tokens = round(chars / 4)
    instr = 0
    for seg in _segments(text):
        core = re.sub(r"[#>*`|_\-]", "", seg).strip()
        if len(core) < 8:
            continue
        if _IMP_START.match(seg) or _DIRECTIVE.search(seg):
            instr += 1
    return chars, tokens, instr


def _plugin_root(arg):
    return (
        arg
        or os.environ.get("CLAUDE_PLUGIN_ROOT")
        or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    )


def gather(plugin_root, project_root, web):
    if web:
        os.environ["GOODFELLOW_PRINCIPLES_WEB"] = "1"
    entries = pc.load_entries(plugin_root=plugin_root, project_root=project_root)
    index = pc.build_index(entries)
    full = pc.emit_principles(plugin_root=plugin_root, project_root=project_root)
    return entries, index, full


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plugin-root", default=None)
    ap.add_argument("--project-root", default=".")
    ap.add_argument("--web", action="store_true", help="Include principles-web.md.")
    ap.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the INDEX exceeds its cap (ratchet mode).",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    plugin_root = _plugin_root(args.plugin_root)
    entries, index, full = gather(plugin_root, args.project_root, args.web)

    i_c, i_tok, i_ins = measure(index)
    f_c, f_tok, f_ins = measure(full)
    n_entries = len(entries)
    tok_cap = INDEX_TOKEN_CAP_WEB if args.web else INDEX_TOKEN_CAP
    entry_cap = INDEX_ENTRY_CAP_WEB if args.web else INDEX_ENTRY_CAP
    over = i_tok > tok_cap or n_entries > entry_cap

    if args.json:
        import json

        print(
            json.dumps(
                {
                    "index": {
                        "tokens": i_tok,
                        "instructions": i_ins,
                        "entries": n_entries,
                    },
                    "full_corpus": {"tokens": f_tok, "instructions": f_ins},
                    "caps": {
                        "index_tokens": tok_cap,
                        "index_entries": entry_cap,
                        "web": args.web,
                    },
                    "over_cap": over,
                },
                indent=2,
            )
        )
    else:
        print("Goodfellow principle density")
        print("-" * 64)
        print(f"{'':<20}{'tokens~':>10}{'instr':>8}{'entries':>9}")
        print(f"{'INDEX (always)':<20}{i_tok:>10}{i_ins:>8}{n_entries:>9}")
        print(f"{'FULL (on demand)':<20}{f_tok:>10}{f_ins:>8}{'':>9}")
        print("-" * 64)
        print(
            f"Index cap ({'core+web' if args.web else 'core'}): {tok_cap} tok / {entry_cap} entries  "
            f"-> {'OVER — displace/merge a principle' if over else 'within budget'}"
        )
        print(
            f"Erosion ceiling {TOKEN_EROSION_LO}-{TOKEN_EROSION_HI} tok; "
            f"cliff {INSTR_CLIFF_LO}-{INSTR_CLIFF_HI} instr (index vs both above)."
        )
        print(
            f"Full corpus is advisory (loads on demand): {f_tok} tok / {f_ins} instr — "
            "not always-loaded."
        )

    if args.check and over:
        print(
            "\nRATCHET: index over cap. Growth is displacement, not accumulation — "
            "a new principle must merge into or subsume an existing one "
            "(docs/instruction-density-budget.md §B).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
