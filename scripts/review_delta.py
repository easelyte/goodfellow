#!/usr/bin/env python3
"""Which commits the final-HEAD check must review, even after a rebase.

`ship` ends its review loop by fixing the last round's findings, so the fix
commits have been reviewed by nobody. The final-HEAD check reviews just those:
the commits after the last reviewed one. That is easy while the last reviewed
commit is an ancestor of HEAD. A rebase rewrites every SHA, though, and
`git diff <last-reviewed>...HEAD` then covers the whole rebased branch plus
whatever the base branch gained meanwhile.

So the last reviewed commit is mapped to its rebased counterpart:

  - the reviewed commits are `merge-base(last, BASE)..last`;
  - the rebased branch must start with exactly those commits, in order, each with
    the same verbatim patch-id (`git patch-id --verbatim`, which ignores only line
    numbers, so a re-indented conflict resolution does not keep the reviewed id);
  - the mapped commit must change the same set of files as the reviewed range,
    and each of them must be byte-identical to the reviewed version. A patch-id
    ignores where a hunk lands, so this is what proves the reviewed code is the
    code on the branch.

Any doubt reviews the whole branch: an unknown SHA, a merge commit in the
branch, a dropped, reordered or rewritten reviewed commit, a file that differs.
Unmapped is unreviewed.

Usage:
  review_delta.py --last <last-reviewed-sha> --base <BASE> [--workdir DIR]

Prints one line, `<sha> <mode> <reason>`:
  ancestor  review <sha>...HEAD (the last reviewed commit is an ancestor)
  rebase    review <sha>...HEAD (<sha> is the reviewed commit's rebased counterpart)
  reviewed  nothing to review: HEAD is the last reviewed commit or its exact rebase
  full      review the whole branch: <sha> is merge-base(HEAD, BASE)

Exit 0 with that line; exit 2 (nothing on stdout) when git fails or BASE is
unknown, which the caller treats as "review the whole branch".
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List, NamedTuple, Set, Tuple


class Delta(NamedTuple):
    base: str
    mode: str  # ancestor | rebase | reviewed | full
    reason: str


class DeltaError(RuntimeError):
    """git could not answer: the caller must review the whole branch."""


def _run(
    repo: Path, args: List[str], stdin: str | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), input=stdin, capture_output=True, text=True
    )


def _git(repo: Path, *args: str) -> str:
    p = _run(repo, list(args))
    if p.returncode != 0:
        raise DeltaError(f"git {' '.join(args)} failed: {p.stderr.strip()[:200]}")
    return p.stdout.strip()


def _patch_ids(repo: Path, rev_range: str) -> List[Tuple[str, str]]:
    """[(commit, verbatim patch-id)] oldest first; an empty commit gets "empty"."""
    out = []
    for c in _git(repo, "rev-list", "--reverse", rev_range).splitlines():
        patch = _git(repo, "diff-tree", "-p", "--no-color", "--no-ext-diff", c)
        p = _run(repo, ["patch-id", "--verbatim"], stdin=patch + "\n")
        if p.returncode != 0:
            raise DeltaError(
                f"git patch-id failed for {c[:10]}: {p.stderr.strip()[:200]}"
            )
        ids = p.stdout.split()
        if not ids and patch.strip():
            raise DeltaError(
                f"git patch-id produced no id for non-empty commit {c[:10]}"
            )
        out.append((c, ids[0] if ids else "empty"))
    return out


def _changed(repo: Path, a: str, b: str) -> Set[str]:
    out = _git(repo, "diff", "-z", "--name-only", "--no-renames", a, b)
    return {p for p in out.split("\0") if p}


def delta_base(repo: Path, last: str, base: str) -> Delta:
    head = _git(repo, "rev-parse", "HEAD")
    whole = _git(repo, "merge-base", "HEAD", base)
    if _run(repo, ["cat-file", "-e", f"{last}^{{commit}}"]).returncode != 0:
        return Delta(whole, "full", "the last reviewed commit is not in this repo")
    last = _git(repo, "rev-parse", f"{last}^{{commit}}")
    if last == head:
        return Delta(head, "reviewed", "HEAD is the last reviewed commit")
    if _run(repo, ["merge-base", "--is-ancestor", last, "HEAD"]).returncode == 0:
        return Delta(
            last, "ancestor", "the last reviewed commit is an ancestor of HEAD"
        )
    if _git(repo, "rev-list", "--merges", f"{whole}..HEAD"):
        return Delta(whole, "full", "a merge commit in the branch")
    old_base = _run(repo, ["merge-base", last, base]).stdout.strip()
    if not old_base:
        return Delta(
            whole, "full", "the last reviewed commit shares no history with the base"
        )
    reviewed = _patch_ids(repo, f"{old_base}..{last}")
    rebased = _patch_ids(repo, f"{whole}..HEAD")
    # In order, one for one, covering EVERY reviewed commit: a dropped, reordered
    # or rewritten reviewed commit changes what the review approved.
    if (
        not reviewed
        or len(rebased) < len(reviewed)
        or any(new[1] != old[1] for new, old in zip(rebased, reviewed))
    ):
        return Delta(
            whole,
            "full",
            "the branch does not start with every reviewed patch, in order",
        )
    mapped = rebased[len(reviewed) - 1][0]
    paths = _changed(repo, old_base, last)
    if paths != _changed(repo, whole, mapped):
        return Delta(
            whole, "full", "the rebased commits change a different set of files"
        )
    differ = sorted(
        p
        for p in _git(
            repo,
            "--literal-pathspecs",
            "diff",
            "-z",
            "--name-only",
            "--no-renames",
            last,
            mapped,
            "--",
            *sorted(paths),
        ).split("\0")
        if p
    )
    if differ:
        return Delta(whole, "full", f"{differ[0]} differs from the reviewed content")
    n = len(reviewed)
    if mapped == head:
        return Delta(
            head, "reviewed", f"pure rebase: all {n} commit(s) map to reviewed ones"
        )
    return Delta(
        mapped, "rebase", f"{n} reviewed commit(s) mapped by patch-id and file content"
    )


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--last", required=True, help="the last reviewed commit")
    ap.add_argument(
        "--base", required=True, help="the branch the PR targets, e.g. origin/main"
    )
    ap.add_argument("--workdir", default=".")
    a = ap.parse_args(argv)
    try:
        d = delta_base(Path(a.workdir).resolve(), a.last, a.base)
    except DeltaError as exc:
        print(f"review-delta: {exc}; review the whole branch", file=sys.stderr)
        return 2
    print(f"{d.base} {d.mode} {d.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
