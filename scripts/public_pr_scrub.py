#!/usr/bin/env python3
"""Pre-PR internal-ref scrub gate for public / not-solely-owned targets.

Before opening a PR to a repo you do not solely control (a fork -> upstream, an
OSS contribution, or any world-visible repo), internal provenance leaks are
world-visible and meaningless-to-misleading in a repo that isn't yours. This gate
scans the PR's ADDED lines against a denylist and BLOCKS (nonzero exit) on any hit.

It reuses goodfellow's own egress matcher (scripts/egress_scan.py) — the same
word-boundary-aware mechanism the CI backstop uses — so the match semantics are
identical everywhere.

The denylist is YOURS to define — this ships with NO built-in inventory of any
particular project's internal names. Resolution order (first that exists wins):

  1. --denylist <path>
  2. $GOODFELLOW_INTERNAL_DENYLIST  (a file path)
  3. <project-root>/.goodfellow/internal_denylist.txt

Denylist file format: one phrase per line; `#` comments; blanks ignored. List
your internal-only tokens — product/service/customer names, internal host names
and absolute paths, internal ticket/PR-number forms, internal rule-id citation
forms, anything that should never ship to a public repo.

Exit codes: 0 clean (or no denylist and not --require-denylist); 1 hits found
(BLOCK the PR); 2 usage / no denylist while --require-denylist.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from egress_scan import load_denylist, phrase_hits  # noqa: E402


def resolve_denylist_path(
    explicit: Optional[str], project_root: Path
) -> Optional[Path]:
    if explicit:
        return Path(explicit)
    env = os.environ.get("GOODFELLOW_INTERNAL_DENYLIST")
    if env:
        return Path(env)
    default = project_root / ".goodfellow" / "internal_denylist.txt"
    if default.exists():
        return default
    return None


def _git(workdir: Path, args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(workdir), *args], capture_output=True, text=True
    )


def default_base(workdir: Path) -> str:
    """merge-base against the upstream tracking branch, else origin/main, else main."""
    up = _git(workdir, ["merge-base", "HEAD", "@{upstream}"])
    if up.returncode == 0 and up.stdout.strip():
        return up.stdout.strip()
    for ref in ("origin/main", "origin/master", "main", "master"):
        if _git(workdir, ["rev-parse", "--verify", "--quiet", ref]).returncode == 0:
            return ref
    return "HEAD~1"


def added_lines(workdir: Path, base: str) -> str:
    """The '+' added lines of `git diff <base>...HEAD` (leading '+' stripped)."""
    proc = _git(workdir, ["diff", f"{base}...HEAD"])
    out: List[str] = []
    for line in proc.stdout.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            out.append(line[1:])
    return "\n".join(out)


def scan_diff(workdir: Path, base: str, denylist: List[str]) -> List[str]:
    text = added_lines(workdir, base)
    return [p for p in denylist if phrase_hits(p, text)]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Pre-PR internal-ref scrub gate")
    parser.add_argument("--base", default=None, help="base ref (default: merge-base)")
    parser.add_argument("--denylist", default=None, help="path to your denylist file")
    parser.add_argument("--workdir", default=".", help="repo working directory")
    parser.add_argument(
        "--require-denylist",
        action="store_true",
        help="fail (exit 2) if no denylist is configured, instead of passing",
    )
    args = parser.parse_args(argv)

    workdir = Path(args.workdir).resolve()
    dl_path = resolve_denylist_path(args.denylist, workdir)
    if dl_path is None or not Path(dl_path).exists():
        msg = (
            "no internal-ref denylist configured "
            "(--denylist / $GOODFELLOW_INTERNAL_DENYLIST / "
            ".goodfellow/internal_denylist.txt)"
        )
        if args.require_denylist:
            print(f"BLOCK: {msg}", file=sys.stderr)
            return 2
        print(f"scrub SKIPPED: {msg}")
        print(
            "Define one to gate public PRs against your own internal names.",
            file=sys.stderr,
        )
        return 0

    denylist = load_denylist(Path(dl_path))
    base = args.base or default_base(workdir)
    hits = scan_diff(workdir, base, denylist)
    if hits:
        print(f"INTERNAL-REF HITS in the diff vs {base} (denylist {dl_path}):")
        for h in hits:
            print(f"  - {h}")
        print(
            "Scrub ALL of them (or none) before opening the PR. A partial scrub "
            "is worse than none.",
            file=sys.stderr,
        )
        return 1
    print(f"scrub clean vs {base} (denylist {dl_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
