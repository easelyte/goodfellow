#!/usr/bin/env python3
"""Resolve the risk tier for a change: the classify step behind `brainstorm` and `ship`.

goodfellow scales the *documents and the review surface* to the risk of a change. The
code checks (a red test for behaviour changes, the review of the diff, the mutation
check on high-stakes paths, the stop list) run at every tier.

    T0 fix       existing behaviour is wrong and can be reproduced
    T1 feature   a new, bounded, reversible behaviour inside an existing model
    T2 design    a new concept, data model or contract; ambiguous intent; or two
                 or more reasonable designs
    T3 live      the change mutates live state: migrations, deploy/cron/services,
                 credentials, data deletion or backup, money, external sends

The model picks a tier from that rubric (`--proposed`); this script applies the rules
the model must not bend:

  * Hard floors. A path listed in the high-stakes list sets a floor of T1. A
    live-state path (built-in defaults plus `.goodfellow/live_state_paths.txt`) or a
    `--live-state REASON` sets a floor of T3.
  * The model may raise a tier, never lower it: the result is at least the floor and
    at least `--previous` (a tier chosen earlier in the same run).
  * The operator may lower a tier with `--tier Tn` (`--quick` is `--tier T0`), but
    never below the floor. Below the floor is refused with the reason, never
    silently honoured.

Usage:

    tier.py resolve (--base REF | --paths P [P ...]) (--proposed Tn | --tier Tn | --quick)
                    [--previous Tn] [--live-state REASON]... [--reason TEXT]
                    [--repo DIR] [--json]

Exit codes: 0 resolved, 3 refused (the requested tier is below a hard floor),
2 could not decide (bad base, unreadable or missing configured path list, bad
arguments). Exit 2 is never a T0: the caller must stop, not assume the lowest tier.

Path lists are one glob per line against the repo-relative path (`*` stays inside a
directory, `**` spans directories, `#` comments). High-stakes list: `--high-stakes`,
else `$GOODFELLOW_HIGH_STAKES_PATHS`, else `.goodfellow/high_stakes_paths.txt` (the
same file the mutation check reads). Live-state list: `$GOODFELLOW_LIVE_STATE_PATHS`,
else `.goodfellow/live_state_paths.txt`; its lines add to the built-in defaults, and a
line `!<glob>` drops a default that does not fit your project.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mutation_check import is_high_stakes, parse_path_list  # noqa: E402

TIERS = ("T0", "T1", "T2", "T3")
TIER_NAMES = {"T0": "fix", "T1": "feature", "T2": "design", "T3": "live"}

# Paths whose change almost always mutates live state. Projects extend or trim this
# with .goodfellow/live_state_paths.txt.
DEFAULT_LIVE_STATE = (
    "**/migrations/**",
    "**/migrate/**",
    "**/*.service",
    "**/*.timer",
    "**/crontab",
    "**/*.crontab",
    "**/*.tf",
)

EXIT_OK, EXIT_ERROR, EXIT_REFUSED = 0, 2, 3


class TierError(Exception):
    """No trustworthy decision is possible (exit 2)."""


def rank(tier: str) -> int:
    return TIERS.index(tier)


def parse_tier(value: Optional[str], flag: str) -> Optional[str]:
    if value is None:
        return None
    if value not in TIERS:
        raise TierError(f"{flag} must be one of {', '.join(TIERS)} (got {value!r})")
    return value


# --------------------------------------------------------------------------- #
# Inputs: changed paths and the two path lists
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise TierError(
            f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.returncode}"
        )
    return proc.stdout


def changed_paths(repo: Path, base: str) -> List[str]:
    """Every path the work touches: committed since the merge base, staged,
    unstaged and untracked. Renames count on both sides (`--no-renames`), so a
    moved or deleted file still raises the floor of the path it left."""
    try:
        _git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    except TierError as exc:
        raise TierError(f"base {base!r} is not a commit in {repo}") from exc
    out = set()
    out.update(
        _git(repo, "diff", "--name-only", "--no-renames", f"{base}...HEAD").split("\n")
    )
    out.update(_git(repo, "diff", "--name-only", "--no-renames", "HEAD").split("\n"))
    out.update(_git(repo, "ls-files", "--others", "--exclude-standard").split("\n"))
    return sorted(p for p in out if p)


def _read_list(path: Path, what: str) -> List[str]:
    try:
        return parse_path_list(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TierError(f"cannot read the {what} list {path}: {exc}") from exc


def high_stakes_patterns(repo: Path, explicit: Optional[str]) -> List[str]:
    configured = explicit or os.environ.get("GOODFELLOW_HIGH_STAKES_PATHS")
    if configured:
        return _read_list(Path(configured), "high-stakes")  # configured: must exist
    default = repo / ".goodfellow" / "high_stakes_paths.txt"
    return _read_list(default, "high-stakes") if default.exists() else []


def live_state_patterns(repo: Path) -> List[str]:
    configured = os.environ.get("GOODFELLOW_LIVE_STATE_PATHS")
    if configured:
        lines = _read_list(Path(configured), "live-state")
    else:
        default = repo / ".goodfellow" / "live_state_paths.txt"
        lines = _read_list(default, "live-state") if default.exists() else []
    dropped = {line[1:].strip() for line in lines if line.startswith("!")}
    added = [line for line in lines if not line.startswith("!")]
    return [p for p in DEFAULT_LIVE_STATE if p not in dropped] + added


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


def floors(
    paths: Sequence[str],
    high_stakes: Sequence[str],
    live_state: Sequence[str],
    live_state_reasons: Sequence[str],
) -> Tuple[str, List[str]]:
    """The hard floor and the human reasons for it (empty when the floor is T0)."""
    reasons: List[str] = []
    floor = "T0"
    live_hits = [p for p in paths if is_high_stakes(p, list(live_state))]
    for p in live_hits:
        reasons.append(f"{p} is a live-state path")
    for r in live_state_reasons:
        reasons.append(f"live state: {r}")
    if live_hits or live_state_reasons:
        floor = "T3"
    stakes_hits = [p for p in paths if is_high_stakes(p, list(high_stakes))]
    for p in stakes_hits:
        reasons.append(f"{p} matches a high-stakes path")
    if stakes_hits and rank(floor) < rank("T1"):
        floor = "T1"
    return floor, reasons


def resolve(
    floor: str,
    proposed: Optional[str],
    override: Optional[str],
    previous: Optional[str],
) -> Tuple[str, str, bool]:
    """Return (tier, source, refused). `source` names what decided the tier."""
    if override is not None:
        if rank(override) < rank(floor):
            return floor, "floor", True
        return override, "override", False
    if proposed is None:
        raise TierError("pass --proposed (the model's pick) or --tier/--quick")
    tier, source = proposed, "proposed"
    if previous is not None and rank(previous) > rank(tier):
        tier, source = previous, "previous"
    if rank(floor) > rank(tier):
        tier, source = floor, "floor"
    return tier, source, False


def cmd_resolve(a: argparse.Namespace) -> int:
    proposed = parse_tier(a.proposed, "--proposed")
    override = parse_tier(a.tier, "--tier")
    previous = parse_tier(a.previous, "--previous")
    if a.quick:
        if override is not None and override != "T0":
            raise TierError(
                "--quick means --tier T0; it conflicts with --tier " + override
            )
        override = "T0"
    if a.base is None and a.paths is None:
        raise TierError(
            "pass --base REF (a diff) or --paths (the files you expect to touch)"
        )

    repo = Path(a.repo or os.getcwd())
    paths = list(a.paths or [])
    if a.base is not None:
        paths = sorted(set(paths) | set(changed_paths(repo, a.base)))
    floor, reasons = floors(
        paths,
        high_stakes_patterns(repo, a.high_stakes),
        live_state_patterns(repo),
        a.live_state or [],
    )
    tier, source, refused = resolve(floor, proposed, override, previous)

    requested = override if refused else None
    result = {
        "tier": tier,
        "name": TIER_NAMES[tier],
        "floor": floor,
        "floor_reasons": reasons,
        "source": source,
        "refused": refused,
        "requested": requested,
        "paths": paths,
    }
    floor_line = (
        f"Floor {floor}: " + "; ".join(reasons) + "."
        if reasons
        else f"Floor {floor}: nothing in the diff is on a high-stakes or live-state path."
    )
    if refused:
        print(
            f"Refused: --tier {requested} is below the {floor} floor. {floor_line}",
            file=sys.stderr,
        )
    if a.json:
        print(json.dumps(result, indent=1))
    else:
        reason = f": {a.reason}" if a.reason else ""
        if not refused:
            print(f"Tier {tier} ({TIER_NAMES[tier]}){reason}")
            print(floor_line)
    return EXIT_REFUSED if refused else EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve goodfellow's risk tier for a change.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser(
        "resolve", help="Apply hard floors and overrides to a proposed tier"
    )
    r.add_argument("--repo", help="Repository root (default: cwd)")
    r.add_argument("--base", help="Diff the work against this ref (merge base)")
    r.add_argument("--paths", nargs="*", help="Paths the work touches or will touch")
    r.add_argument("--proposed", help="The tier the model picked from the rubric")
    r.add_argument("--tier", help="Operator override: T0..T3, refused below the floor")
    r.add_argument("--quick", action="store_true", help="Alias for --tier T0")
    r.add_argument(
        "--previous", help="A tier chosen earlier in this run (never lowered)"
    )
    r.add_argument(
        "--live-state",
        action="append",
        metavar="REASON",
        help="The change mutates live state (sets a T3 floor); repeatable",
    )
    r.add_argument(
        "--reason", help="One line: why this tier (shown in the announcement)"
    )
    r.add_argument(
        "--high-stakes", help="High-stakes path list (overrides the default)"
    )
    r.add_argument("--json", action="store_true", help="Machine-readable output")
    try:
        a = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_ERROR if exc.code else EXIT_OK
    try:
        return cmd_resolve(a)
    except TierError as exc:
        print(f"tier: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
