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
(BLOCK the PR); 2 fail-closed — usage, no denylist while --require-denylist, OR
the diff could not be computed (bad/unknown base, shallow clone, non-repo) so
nothing was actually scanned. Without --base the gate diffs against EVERY one of
upstream/HEAD, upstream/main, upstream/master, origin/HEAD, origin/main,
origin/master, main, master that exists and does not already contain HEAD, since
it cannot tell which one the PR targets; when none does, that is exit 2 too: the
gate will not guess a narrower base. Pass --base to scan against the exact target.
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


class ScrubError(RuntimeError):
    """The diff to scan could not be computed — the gate must fail CLOSED.

    A security gate that reports 'clean' when it in fact scanned NOTHING (an
    invalid/unknown base, shallow history with no merge-base, a non-repo) is
    worse than useless: it authorizes publishing without inspection. Raising
    this drives the CLI to the same fail-closed exit code (2) as a missing
    denylist under --require-denylist.
    """


def _git(workdir: Path, args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(workdir), *args], capture_output=True, text=True
    )


DEFAULT_BASE_REFS = (
    "upstream/HEAD",
    "upstream/main",
    "upstream/master",
    "origin/HEAD",
    "origin/main",
    "origin/master",
    "main",
    "master",
)


def default_bases(workdir: Path) -> List[str]:
    """Every ref in DEFAULT_BASE_REFS that exists and does not already contain
    HEAD, in that order. Without --base the gate scans against ALL of them.

    Why all: the gate cannot tell which branch the PR will target. In a fork
    layout the PR may go to `upstream/main` or to `origin/main`, and the two can
    sit at different commits; scanning against only one can miss commits the
    other target would receive. Scanning against every candidate covers
    whichever is meant. The cost is a possible false positive on a commit that
    one candidate already has, which blocks rather than leaks. Pass --base to
    scan against the exact target.

    A ref that contains HEAD (the branch's own pushed copy, or the default
    branch when HEAD is on it) gives an empty `<base>...HEAD` diff, so it is
    skipped. For the same reason the branch's upstream tracking ref is never
    used: a pushed feature branch tracks itself.

    FAIL-CLOSED: when no candidate is left there is no honest base to diff
    against. Guessing one (the tip's parent, say) would scan only the last
    commit, so a leak in any earlier commit of the branch would pass as clean.
    Raise ScrubError instead and let the caller pass --base explicitly.
    """
    head = _git(workdir, ["rev-parse", "--verify", "--quiet", "HEAD"])
    if head.returncode != 0 or not head.stdout.strip():
        raise ScrubError("cannot resolve HEAD; is this a git repository?")
    head_sha = head.stdout.strip()
    bases: List[str] = []
    for ref in DEFAULT_BASE_REFS:
        if _git(workdir, ["rev-parse", "--verify", "--quiet", ref]).returncode != 0:
            continue
        mb = _git(workdir, ["merge-base", ref, "HEAD"])
        if mb.returncode != 0 or not mb.stdout.strip():
            # An existing target we cannot compare against (a shallow clone,
            # unrelated history) must not be dropped: the other candidates'
            # clean scans would then stand in for it.
            raise ScrubError(
                f"cannot compute the merge-base of {ref} and HEAD (shallow "
                "clone or unrelated history?); fetch more history or pass --base"
            )
        if mb.stdout.strip() != head_sha:
            bases.append(ref)
    if not bases:
        raise ScrubError(
            "cannot determine the base to diff against (none of "
            f"{', '.join(DEFAULT_BASE_REFS)} exists without already containing "
            "HEAD); pass --base <the PR's target branch> explicitly"
        )
    return bases


def added_lines(workdir: Path, base: str) -> str:
    """The '+' added lines of `git diff <base>...HEAD` (leading '+' stripped).

    FAIL-CLOSED: `git diff` returns nonzero ONLY on error (a bad/unknown base, a
    shallow clone with no merge-base, a non-repo) — a non-empty diff still exits
    0. So a nonzero return code means the scan target could not be built; raise
    ScrubError rather than scan the (empty) stdout and falsely report clean.
    """
    # Plain unified diff whatever the user's config says: forced color would
    # hide every `+` and `@@` behind escape codes, and an external diff tool
    # replaces the unified format entirely. Textconv stays on: a configured
    # converter is the only way a binary document's text reaches this scan.
    proc = _git(
        workdir, ["diff", "--no-color", "--no-ext-diff", f"{base}...HEAD"]
    )
    if proc.returncode != 0:
        raise ScrubError(
            f"git diff against {base!r} failed (rc={proc.returncode}): "
            f"{proc.stderr.strip() or 'unknown git error'}"
        )
    out: List[str] = []
    in_hunk = False
    for line in proc.stdout.splitlines():
        # Track hunks rather than skipping every `+++` line: a content line
        # `++X` is `+++X` in the diff, the same prefix as the `+++ b/file`
        # header. Headers only occur between `diff --git` and the first `@@`.
        if line.startswith("diff --git "):
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            out.append(line[1:])
    return "\n".join(out)


def scan_diff(workdir: Path, base: str, denylist: List[str]) -> List[str]:
    text = added_lines(workdir, base)
    return [p for p in denylist if phrase_hits(p, text)]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Pre-PR internal-ref scrub gate")
    parser.add_argument(
        "--base",
        default=None,
        help="base ref: the PR target (default: see DEFAULT_BASE_REFS)",
    )
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
    try:
        # Resolving the default bases runs git too, so it sits inside the
        # fail-closed block: an error there must exit 2, not escape as a
        # traceback (exit 1, which means "hits found").
        bases = [args.base] if args.base else default_bases(workdir)
        base = ", ".join(bases)
        found = set()
        for b in bases:
            found.update(scan_diff(workdir, b, denylist))
        hits = [p for p in denylist if p in found]
    except ScrubError as exc:
        # Fail CLOSED: never report clean when the diff could not be computed.
        print(f"BLOCK: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"BLOCK: could not run git ({exc})", file=sys.stderr)
        return 2
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
