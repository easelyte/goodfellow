#!/usr/bin/env python3
"""Dedup learnings against shipped principles (M4 / B-R2-2, P2).

Exact normalized match only — NOT fuzzy/semantic (documented limitation,
consistent with the "no selective/semantic recall" non-goal). Catches
textual/near-textual restatements; a semantically-different rewording is NOT
caught (e.g. "validate webhook payload shape" vs P-008 "Guard Boundary Inputs").
The tradeoff is deliberate: a rare semantic duplicate is tolerable; a fuzzy
threshold that silently drops genuine project-specific learnings is not.
"""

import re
import sys


def _normalize(text):
    """Unicode casefold (MN-C — not ASCII lower()), strip punctuation, collapse
    whitespace."""
    text = text.casefold()
    text = re.sub(r"[^\w\s]", " ", text)  # strip punctuation
    text = re.sub(r"\s+", " ", text).strip()
    return text


def match_shipped_principle(description, shipped):
    """Return the matched `P-NNN` (so the skill can cite it) or None.

    `shipped` = list of (p_id, rule_line). Exact normalized equality only."""
    norm = _normalize(description)
    if not norm:
        return None
    for p_id, rule_line in shipped:
        if norm == _normalize(rule_line):
            return p_id
    return None


def parse_shipped(principles_text):
    """Extract (p_id, rule_line) from `### P-NNN. <title>` + the following
    blockquote rule line in knowledge/principles*.md."""
    out = []
    lines = principles_text.splitlines()
    i = 0
    while i < len(lines):
        m = re.match(r"^### (P-\d{3})\.\s*(.*)$", lines[i])
        if m:
            p_id = m.group(1)
            rule = None
            # the rule is the next non-empty blockquote line
            j = i + 1
            while j < len(lines):
                stripped = lines[j].strip()
                if stripped.startswith(">"):
                    rule = stripped.lstrip("> ").strip()
                    break
                if stripped == "":
                    j += 1
                    continue
                break
            out.append((p_id, rule if rule is not None else m.group(2)))
        i += 1
    return out


def main(argv=None):
    """CLI: read principles file(s) on stdin-paths, match a description.
    Usage: dedup_principles.py --description "..." --principles f1.md [f2.md ...]
    Prints the matched P-NNN to stdout (exit 0) or nothing (exit 0). Errors to
    stderr (exit 1)."""
    import argparse
    import pathlib

    parser = argparse.ArgumentParser(description="Dedup vs shipped principles")
    parser.add_argument("--description", required=True)
    parser.add_argument("--principles", nargs="+", required=True)
    args = parser.parse_args(argv)
    try:
        text = "\n".join(pathlib.Path(p).read_text() for p in args.principles)
    except OSError as e:
        print(str(e), file=sys.stderr, flush=True)
        return 1
    shipped = parse_shipped(text)
    if not shipped:
        # fail CLOSED (CM-R3): zero principles parsed from required inputs means
        # path/format drift — error rather than silently returning "no match" (which
        # would let a restatement of a shipped principle get persisted as new).
        print(
            f"no shipped principles parsed from {args.principles} "
            "— refusing to dedup-pass (fail closed)",
            file=sys.stderr,
            flush=True,
        )
        return 1
    pid = match_shipped_principle(args.description, shipped)
    if pid:
        print(pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
