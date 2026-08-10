#!/usr/bin/env python3
"""Full-file + enclosing-context + token-grep assembly (D2 context builder).

Upgrades the adversarial code-review context builder beyond hunk-only:

  * Full-file content, scoped per mode. Added/Modified/Renamed-target files are
    inlined at the mode's ref (`git show HEAD:<path>` for --diff, `git show
    <sha>:<path>` for --commit). DELETED paths are status-guarded and skipped
    with a note (never `git show <ref>:<deleted>`, which would abort under a
    `set -euo pipefail` caller). Governed by FULLFILE_BUDGET: largest-diff
    first, whole-file omission (never mid-file truncation).

  * Exported-symbol caller-grep: `git grep -n -F -e <sym> <rev> --` (mode-scoped).

  * Literal token cross-reference grep: extracts CSS custom-props, utility class
    fragments, and quoted config-key/env/route literals from ADDED/CHANGED diff
    lines, deterministically sorts + caps them, and greps each with
    `git grep -n -F -e "<tok>" <rev> --` (the `-e ... --` form — a bare
    `--surface-canvas` token errors "unknown option" / exit 129).

All greps are OPTION-SAFE (`-e <pat> <rev> --`) and MODE-SCOPED (commit→sha,
diff→HEAD) so a historical --commit review never resolves against the wrong bytes.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

FULLFILE_BUDGET = 120_000  # chars (~30k tokens); tunable via --fullfile-budget
TOKEN_SELECT_CAP = 30  # first-N tokens greppable
CROSSREF_LINE_CAP = 60  # total inlined cross-ref file:line lines

# Token extraction grammar (ordered classes; a token is assigned to its FIRST
# matching class). Applied only to added/changed diff lines.
_CLASS_PATTERNS: Tuple[Tuple[int, "re.Pattern[str]"], ...] = (
    (1, re.compile(r"--[a-z][a-z0-9-]*")),  # CSS custom property
    (
        2,
        re.compile(r"(?:text|bg|border|ring|fill|stroke)-[a-z][a-z0-9-]*"),
    ),  # utility class
    (3, re.compile(r'"[A-Z0-9_]{3,}"')),  # config key / env var name
    (3, re.compile(r'"/[a-z0-9/_-]+"')),  # route path
)

# Exported-symbol extraction (py + js/ts) from added lines.
_SYMBOL_PATTERNS: Tuple["re.Pattern[str]", ...] = (
    re.compile(r"^(?:async\s+)?def\s+([A-Za-z_]\w*)"),
    re.compile(r"^class\s+([A-Za-z_]\w*)"),
    re.compile(r"export\s+(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"export\s+(?:const|let|var|class)\s+([A-Za-z_$][\w$]*)"),
)


@dataclass(frozen=True)
class Token:
    cls: int
    value: str  # the literal to grep (unquoted for class-3)


def _git(workdir: Path, args: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(workdir), *args],
        capture_output=True,
        text=True,
    )


def _added_lines(
    mode: str, workdir: Path, rev: str, diff_range: Optional[str], paths: Sequence[str]
) -> List[str]:
    """Return added/changed lines (leading '+' stripped) from the reviewed diff."""
    if mode == "commit":
        args = ["show", "--format=", "--unified=0", rev, "--", *paths]
    elif mode == "diff":
        args = ["diff", "--unified=0", diff_range or "HEAD", "--", *paths]
    else:
        return []
    proc = _git(workdir, args)
    out: List[str] = []
    for line in proc.stdout.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            out.append(line[1:])
    return out


def _diff_sizes(
    mode: str, workdir: Path, rev: str, diff_range: Optional[str], paths: Sequence[str]
) -> Dict[str, int]:
    """Per-path changed-line counts (added+deleted) for largest-diff-first order."""
    if mode == "commit":
        args = ["show", "--numstat", "--format=", rev, "--", *paths]
    elif mode == "diff":
        args = ["diff", "--numstat", diff_range or "HEAD", "--", *paths]
    else:
        return {}
    proc = _git(workdir, args)
    sizes: Dict[str, int] = {}
    for line in proc.stdout.splitlines():
        cols = line.split("\t")
        if len(cols) < 3:
            continue
        added = int(cols[0]) if cols[0].isdigit() else 0
        deleted = int(cols[1]) if cols[1].isdigit() else 0
        sizes[cols[2]] = added + deleted
    return sizes


def _show_file(workdir: Path, ref: str, path: str) -> Optional[str]:
    """Status-guarded `git show <ref>:<path>`; None if the blob is absent."""
    proc = _git(workdir, ["show", f"{ref}:{path}"])
    if proc.returncode != 0:
        return None
    return proc.stdout


GREP_HIT_CAP = 50  # hard cap on hits per token — a common symbol/string can match
#                    thousands of repo lines; bound the fan-out at the source so a
#                    single grep can never blow the prompt past codex's char limit.


def _grep(workdir: Path, rev: str, token: str, limit: int = GREP_HIT_CAP) -> List[str]:
    """Option-safe, mode-scoped `git grep -n -F -e <token> <rev> --`.

    Returns at most `limit` `path:line` strings. Never raises: git grep exit 1 =
    no matches, exit 0 = matches; anything else is treated as no matches.
    """
    proc = _git(workdir, ["grep", "-n", "-F", "-e", token, rev, "--"])
    if proc.returncode not in (0, 1):
        return []
    hits: List[str] = []
    for line in proc.stdout.splitlines():
        # Format: <rev>:<path>:<lineno>:<content>
        parts = line.split(":", 3)
        if len(parts) < 3:
            continue
        _rev, path, lineno = parts[0], parts[1], parts[2]
        hits.append(f"{path}:{lineno}")
        if len(hits) >= limit:
            break
    return hits


# ---------------------------------------------------------------------------
# Token / symbol extraction
# ---------------------------------------------------------------------------


def extract_tokens(added_lines: Sequence[str]) -> List[Token]:
    """Extract, dedupe, and deterministically order literal cross-ref tokens."""
    seen: Dict[str, int] = {}  # value → class (first match wins)
    for line in added_lines:
        for cls, pat in _CLASS_PATTERNS:
            for m in pat.finditer(line):
                raw = m.group(0)
                value = raw.strip('"') if cls == 3 else raw
                if value not in seen:
                    seen[value] = cls
    tokens = [Token(cls=c, value=v) for v, c in seen.items()]
    tokens.sort(key=lambda t: (t.cls, t.value))
    return tokens


def extract_symbols(added_lines: Sequence[str]) -> List[str]:
    seen: List[str] = []
    for line in added_lines:
        stripped = line.strip()
        for pat in _SYMBOL_PATTERNS:
            m = pat.search(stripped)
            if m:
                name = m.group(1)
                if name not in seen:
                    seen.append(name)
    return sorted(seen)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _fullfile_section(
    mode: str,
    workdir: Path,
    rev: str,
    diff_range: Optional[str],
    changed_files: Sequence[Tuple[str, str]],
    budget: int,
) -> str:
    if mode == "files":
        return ""  # existing path already inlines whole bodies

    deletions: List[str] = []
    candidates: List[Tuple[str, str]] = []  # (path, content)
    for status, path in changed_files:
        su = str(status).upper()
        if su.startswith("D"):
            deletions.append(path)
            continue
        content = _show_file(workdir, rev, path)
        if content is None:
            # Status-guard: blob absent at ref (e.g. a rename's old path) → note.
            deletions.append(path)
            continue
        candidates.append((path, content))

    sizes = _diff_sizes(mode, workdir, rev, diff_range, [p for p, _ in candidates])
    # largest-diff-first, tie-break by path for determinism.
    candidates.sort(key=lambda pc: (-sizes.get(pc[0], 0), pc[0]))

    parts: List[str] = ["## Full changed-file context (reviewed revision)", ""]
    running = 0
    omitted: List[str] = []
    for path, content in candidates:
        if running + len(content) > budget:
            omitted.append(path)  # whole-file omission; never truncate
            continue
        running += len(content)
        parts.append(f"### {path}")
        parts.append("```")
        parts.append(content.rstrip("\n"))
        parts.append("```")
        parts.append("")

    for path in deletions:
        parts.append(f"### {path} — deleted in this change (no full-file context)")
        parts.append("")
    if omitted:
        parts.append("Context-omitted (Read on demand): " + ", ".join(sorted(omitted)))
        parts.append("")
    return "\n".join(parts).rstrip("\n") + "\n"


CALLER_SYMBOL_CAP = 20  # bound the number of exported symbols grepped
CALLER_HITS_PER_SYMBOL = 10  # bound the callers shown per symbol


def _callergrep_section(
    workdir: Path, rev: str, symbols: Sequence[str], changed_paths: Sequence[str]
) -> str:
    changed = set(changed_paths)
    parts: List[str] = []
    overflow_syms = list(symbols[CALLER_SYMBOL_CAP:])
    for sym in symbols[:CALLER_SYMBOL_CAP]:
        hits = [
            h
            for h in _grep(workdir, rev, sym, limit=CALLER_HITS_PER_SYMBOL + 1)
            if h.split(":")[0] not in changed
        ]
        if hits:
            shown = hits[:CALLER_HITS_PER_SYMBOL]
            suffix = (
                " …(+more, Read on demand)"
                if len(hits) > CALLER_HITS_PER_SYMBOL
                else ""
            )
            parts.append(f"- {sym}: " + ", ".join(shown) + suffix)
    if not parts:
        return ""
    tail = ""
    if overflow_syms:
        tail = (
            f"\n+{len(overflow_syms)} more symbols (Read on demand): "
            + ", ".join(sorted(overflow_syms))
            + "\n"
        )
    return "## Exported-symbol callers\n\n" + "\n".join(parts) + "\n" + tail


def _crossref_section(
    workdir: Path, rev: str, tokens: Sequence[Token], changed_paths: Sequence[str]
) -> str:
    changed = set(changed_paths)
    selected = list(tokens[:TOKEN_SELECT_CAP])
    overflow_tokens = [t.value for t in tokens[TOKEN_SELECT_CAP:]]

    lines: List[str] = []
    line_budget_hit = False
    inlined_line_count = 0
    for tok in selected:
        hits = [
            h for h in _grep(workdir, rev, tok.value) if h.split(":")[0] not in changed
        ]
        if not hits:
            continue
        remaining = CROSSREF_LINE_CAP - inlined_line_count
        if remaining <= 0:
            line_budget_hit = True
            break
        shown = hits[:remaining]
        inlined_line_count += len(shown)
        lines.append(f"- {tok.value}: " + ", ".join(shown))
        if len(shown) < len(hits):
            line_budget_hit = True

    if not lines and not overflow_tokens:
        return ""

    parts: List[str] = ["## Literal token cross-references", ""]
    parts.extend(lines)
    if overflow_tokens:
        parts.append(
            f"+{len(overflow_tokens)} more token cross-refs (Read on demand): "
            + ", ".join(sorted(overflow_tokens))
        )
    if line_budget_hit:
        parts.append(
            f"(cross-ref line cap {CROSSREF_LINE_CAP} reached; Read on demand)"
        )
    return "\n".join(parts).rstrip("\n") + "\n"


def assemble_context(
    *,
    mode: str,
    workdir: Path,
    rev: str,
    changed_files: Sequence[Tuple[str, str]],
    diff_range: Optional[str] = None,
    fullfile_budget: int = FULLFILE_BUDGET,
) -> str:
    """Assemble the full-file + cross-ref context block. mode: diff|commit|files."""
    workdir = Path(workdir)
    changed_paths = [p for _, p in changed_files]

    sections: List[str] = []
    full = _fullfile_section(
        mode, workdir, rev, diff_range, changed_files, fullfile_budget
    )
    if full.strip():
        sections.append(full)

    if mode in {"diff", "commit"}:
        non_deleted = [
            p for (s, p) in changed_files if not str(s).upper().startswith("D")
        ]
        added = _added_lines(mode, workdir, rev, diff_range, non_deleted)
        symbols = extract_symbols(added)
        tokens = extract_tokens(added)
        caller = _callergrep_section(workdir, rev, symbols, changed_paths)
        if caller.strip():
            sections.append(caller)
        crossref = _crossref_section(workdir, rev, tokens, changed_paths)
        if crossref.strip():
            sections.append(crossref)

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_changed(values: Sequence[str]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for raw in values:
        if "\t" in raw:
            # git --name-status is TAB-delimited. A rename/copy emits three
            # fields — `R<score>\t<src>\t<dst>` (likewise `C<score>...`) — and the
            # TARGET path is the LAST field. A plain change is `<status>\t<path>`.
            # Splitting on the FIRST tab only left the target as `<src>\t<dst>`,
            # so `git show <rev>:<that>` failed and the renamed file silently
            # dropped from context.
            fields = raw.split("\t")
            status, path = fields[0], fields[-1]
        else:
            status, _, path = raw.partition(":")
        out.append((status.strip(), path.strip()))
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Full-file + token-grep context assembly"
    )
    parser.add_argument("--mode", required=True, choices=["diff", "commit", "files"])
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--rev", required=True, help="ref to show/grep (HEAD or <sha>)")
    parser.add_argument(
        "--diff-range", default=None, help="git diff spec for --diff mode"
    )
    parser.add_argument("--changed", action="append", default=[])
    parser.add_argument("--fullfile-budget", type=int, default=FULLFILE_BUDGET)
    args = parser.parse_args(argv)

    text = assemble_context(
        mode=args.mode,
        workdir=Path(args.workdir),
        rev=args.rev,
        changed_files=_parse_changed(args.changed),
        diff_range=args.diff_range,
        fullfile_budget=args.fullfile_budget,
    )
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
