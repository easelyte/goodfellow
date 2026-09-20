#!/usr/bin/env python3
"""Goodfellow PreToolUse guard engine — tool-layer enforcement of block rules.

A constraint whose violation is expensive to reverse does NOT belong in prose.
Compaction is optimized for task accuracy, so nothing measures whether a "never do
X" instruction survives the rewrite ("Governance Decay", arxiv 2606.22528). The
enforcement that works is a PreToolUse hook, not a stronger sentence — it fires
deterministically on every tool call regardless of what the context still holds.

This engine backs a single PreToolUse hook (see hooks/hooks.json) and evaluates:

  1. Built-in universal guards (no project knowledge required):
       - `git add -A` / `git add .` / `git add --all`   (stage specific files)
       - the `--dangerously-skip-permissions` CLI flag    (keep the permission flow)
       - force-push to a protected branch                 (main/master by default)
  2. Declarative user BLOCK rules from `.goodfellow/guards.json`, so a project's
     own expensive-to-reverse rules get tool-layer enforcement instead of prose.

## The deny contract (assert JSON, not exit code)

A PreToolUse hook DENIES by printing a permissionDecision JSON object on stdout
and exiting 0. A non-zero exit is a *hook error*, not a deny. Tests must assert
the JSON, never the exit code. See `emit_deny` / `decision_for_input`.

## The "flag text anywhere" caveat

Grepping the whole command string means merely *writing* a blocked flag as text
(e.g. `git commit -m "docs: mention --dangerously-skip-permissions"`) trips the
guard. Two defenses are baked in here:
  - Built-in guards only inspect the `Bash` tool's command, never Write/Edit
    *content*. Documenting or testing a flag in a file never trips a built-in.
  - Matching is shlex-token based, not substring: the flag must appear as its own
    argument token. A flag mentioned inside a quoted commit message is one token
    (the message) and does not match.

## Failure posture (fail-safe-open for the live hook, fail-loud on demand)

A governance gate must not *itself* block legitimate work. If `.goodfellow/guards.json`
is malformed, the live hook skips the user rules (built-ins still enforce) and
writes a warning to stderr — it never deny-alls the session into a deadlock where
you cannot even edit the file to fix it. For a loud, CI/snap-compact-style check,
run `guard_engine.py --validate`, which exits non-zero on a bad config, and
`guard_engine.py --selfcheck`, which prints the active guard set so a
post-compaction session can assert governance survived the boundary.

Escape hatches: `CLAUDE_HOOK_BYPASS=1` (all guards off), `GOODFELLOW_GUARDS=0`
(built-ins off), or a per-rule `bypass_env` in guards.json.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import PurePosixPath, PureWindowsPath
from typing import Iterable, List, Optional, Sequence

DEFAULT_PROTECTED_BRANCHES = ("main", "master")
BUILTIN_IDS = ("git-add-all", "dangerous-skip-permissions", "force-push-protected")
SKIP_PERMS_FLAG = "--dangerously-skip-permissions"


class GuardConfigError(Exception):
    """Raised when .goodfellow/guards.json is present but unusable (fail-loud path)."""


# --------------------------------------------------------------------------- #
# Command tokenization (shared by every built-in guard)
# --------------------------------------------------------------------------- #

_CONTROL_TOKENS = {"&&", "||", ";", "&", "|", "(", ")"}


def split_segments(command: str) -> List[List[str]]:
    """Split a shell command into argument segments at control operators.

    `git add -A && rm x` -> [["git","add","-A"], ["rm","x"]]. Uses shlex in POSIX
    mode so quotes are honored: text inside a quoted string stays a single token
    and never masquerades as a standalone flag argument.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars="();|&")
    lexer.whitespace_split = True
    tokens = list(lexer)
    segments: List[List[str]] = []
    current: List[str] = []
    for token in tokens:
        if token in _CONTROL_TOKENS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _basename_is(token: str, names: set) -> bool:
    """True when token is a bare name in `names`, or a path whose basename is."""
    if token in names:
        return True
    if token.startswith("/") or token.startswith("./") or token.startswith("../"):
        return PurePosixPath(token).name in names
    if re.match(r"^[A-Za-z]:[\\/]", token) or "\\" in token:
        return PureWindowsPath(token).name.lower() in {n.lower() for n in names}
    return False


def _git_arg_start(tokens: List[str]) -> Optional[int]:
    """Index of the git *subcommand*, skipping a leading `command` and git's own
    top-level options (`-C <dir>`, `--git-dir=…`, `--work-tree=…`). None if the
    segment is not a git invocation."""
    index = 0
    if index < len(tokens) and tokens[index] == "command":
        index += 1
    if index >= len(tokens) or not _basename_is(tokens[index], {"git", "git.exe"}):
        return None
    index += 1
    while index < len(tokens):
        token = tokens[index]
        if token in {"-C", "--git-dir", "--work-tree"}:
            index += 2
            continue
        if token.startswith("-C") and token != "-C":
            index += 1
            continue
        if token.startswith("--git-dir=") or token.startswith("--work-tree="):
            index += 1
            continue
        break
    return index


# --------------------------------------------------------------------------- #
# Built-in universal guards — each returns a deny reason string, or None
# --------------------------------------------------------------------------- #


def check_git_add_all(segments: Sequence[List[str]]) -> Optional[str]:
    for tokens in segments:
        start = _git_arg_start(tokens)
        if start is None or start >= len(tokens) or tokens[start] != "add":
            continue
        if any(arg in {"-A", "--all", "."} for arg in tokens[start + 1 :]):
            return (
                "Blocked `git add -A` / `git add .` / `git add --all`. Stage "
                "specific files instead — a blanket add is how secrets and stray "
                "artifacts leak into a commit."
            )
    return None


def check_skip_permissions(segments: Sequence[List[str]]) -> Optional[str]:
    for tokens in segments:
        for token in tokens:
            if token == SKIP_PERMS_FLAG or token.startswith(SKIP_PERMS_FLAG + "="):
                return (
                    f"Blocked `{SKIP_PERMS_FLAG}`. This flag disables the permission "
                    "prompt for every tool call — the whole safety surface. Use the "
                    "normal permission flow."
                )
    return None


def check_force_push_protected(
    segments: Sequence[List[str]], protected: Sequence[str]
) -> Optional[str]:
    """Deny a force-push whose refspec names a protected branch.

    Deliberately scoped to a protected branch appearing as an argument token:
    force-pushing a *feature* branch is routine and legitimate, so a bare
    `git push -f` (no branch named) is NOT blocked — only an explicit
    `git push --force origin main` (or `main`/`master` as a refspec) is.
    """
    protected_set = {b for b in protected if b}
    for tokens in segments:
        start = _git_arg_start(tokens)
        if start is None or start >= len(tokens) or tokens[start] != "push":
            continue
        args = tokens[start + 1 :]
        forced = any(
            a in {"-f", "--force"}
            or a == "--force-with-lease"
            or a.startswith("--force-with-lease=")
            or a.startswith("--force-if-includes")
            for a in args
        )
        if not forced:
            continue
        for a in args:
            if a.startswith("-"):
                continue
            # refspec forms: `main`, `HEAD:main`, `+main`, `origin main`
            ref = a.lstrip("+").split(":")[-1]
            if ref in protected_set:
                return (
                    f"Blocked force-push to protected branch '{ref}'. Force-pushing "
                    f"{'/'.join(sorted(protected_set))} rewrites shared history and "
                    "can destroy other people's commits. Push a feature branch and "
                    "open a PR, or drop --force."
                )
    return None


# --------------------------------------------------------------------------- #
# Declarative user BLOCK rules (.goodfellow/guards.json)
# --------------------------------------------------------------------------- #


def load_config(project_dir: str) -> dict:
    """Read `.goodfellow/guards.json`. Absent file -> {} (fine, built-ins only).
    Present-but-broken -> GuardConfigError (fail-loud path)."""
    path = os.path.join(project_dir, ".goodfellow", "guards.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise GuardConfigError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise GuardConfigError(
            f"{path} must be a JSON object, got {type(data).__name__}"
        )
    validate_config(data, path)
    return data


def validate_config(data: dict, path: str = "guards.json") -> None:
    """Fail loud on a structurally invalid config so typos never silently disarm a rule."""
    protected = data.get("protected_branches", list(DEFAULT_PROTECTED_BRANCHES))
    if not isinstance(protected, list) or not all(
        isinstance(b, str) for b in protected
    ):
        raise GuardConfigError(
            f"{path}: 'protected_branches' must be a list of strings"
        )
    disabled = data.get("disable_builtins", [])
    if not isinstance(disabled, list) or not all(isinstance(b, str) for b in disabled):
        raise GuardConfigError(f"{path}: 'disable_builtins' must be a list of strings")
    for bad in [b for b in disabled if b not in BUILTIN_IDS]:
        raise GuardConfigError(
            f"{path}: unknown built-in '{bad}' in 'disable_builtins' "
            f"(known: {', '.join(BUILTIN_IDS)})"
        )
    rules = data.get("block", [])
    if not isinstance(rules, list):
        raise GuardConfigError(f"{path}: 'block' must be a list")
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise GuardConfigError(f"{path}: block[{i}] must be an object")
        for req in ("id", "pattern", "reason"):
            if not isinstance(rule.get(req), str) or not rule.get(req):
                raise GuardConfigError(
                    f"{path}: block[{i}] missing required string '{req}'"
                )
        match = rule.get("match", "substring")
        if match not in {"substring", "regex"}:
            raise GuardConfigError(
                f"{path}: block[{i}].match must be 'substring' or 'regex' (got {match!r})"
            )
        if match == "regex":
            try:
                re.compile(rule["pattern"])
            except re.error as exc:
                raise GuardConfigError(
                    f"{path}: block[{i}] invalid regex: {exc}"
                ) from exc
        tools = rule.get("tools", ["Bash"])
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise GuardConfigError(
                f"{path}: block[{i}].tools must be a list of strings"
            )


def check_user_rules(text: str, tool_name: str, rules: Iterable[dict]) -> Optional[str]:
    """Evaluate declarative BLOCK rules against the extracted text for this tool."""
    for rule in rules:
        tools = rule.get("tools", ["Bash"])
        if tool_name not in tools:
            continue
        bypass_env = rule.get("bypass_env")
        if bypass_env and os.environ.get(bypass_env) == "1":
            continue
        pattern = rule["pattern"]
        match = rule.get("match", "substring")
        hit = (
            pattern in text
            if match == "substring"
            else re.search(pattern, text, _regex_flags(rule.get("flags", "")))
            is not None
        )
        if hit:
            return f"Blocked by goodfellow guard '{rule['id']}': {rule['reason']}"
    return None


def _regex_flags(flags: str) -> int:
    out = 0
    if "i" in flags:
        out |= re.IGNORECASE
    if "m" in flags:
        out |= re.MULTILINE
    if "s" in flags:
        out |= re.DOTALL
    return out


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def extract_text(tool_name: str, tool_input: dict) -> str:
    """The text a guard inspects for a given tool. Built-ins only ever see Bash
    commands; user rules may target Write/Edit content too."""
    if tool_name == "Write":
        return tool_input.get("content", "") or ""
    if tool_name == "Edit":
        return tool_input.get("new_string", "") or ""
    return tool_input.get("command", "") or ""


def evaluate_builtins(
    command: str, protected: Sequence[str], disabled: Sequence[str]
) -> Optional[str]:
    """Run the built-in universal guards over a Bash command string."""
    if os.environ.get("GOODFELLOW_GUARDS") == "0":
        return None
    try:
        segments = split_segments(command)
    except ValueError:
        # Unparseable shell (unbalanced quotes): don't guess, don't block.
        return None
    checks = []
    if "git-add-all" not in disabled:
        checks.append(lambda: check_git_add_all(segments))
    if "dangerous-skip-permissions" not in disabled:
        checks.append(lambda: check_skip_permissions(segments))
    if "force-push-protected" not in disabled:
        checks.append(lambda: check_force_push_protected(segments, protected))
    for check in checks:
        reason = check()
        if reason:
            return reason
    return None


def decision_for_input(hook_input: dict, project_dir: str) -> Optional[str]:
    """Return a deny reason for a PreToolUse payload, or None to allow.

    Pure (no stdout/exit): the unit under test. Fail-safe-open on a malformed
    user config — built-ins still run, user rules are skipped, warning to stderr.
    """
    if os.environ.get("CLAUDE_HOOK_BYPASS") == "1":
        return None
    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {}) or {}

    try:
        config = load_config(project_dir)
    except GuardConfigError as exc:
        print(f"goodfellow guard_engine: {exc} (user rules skipped)", file=sys.stderr)
        config = {}

    protected = config.get("protected_branches", list(DEFAULT_PROTECTED_BRANCHES))
    disabled = config.get("disable_builtins", [])

    # Built-in universal guards only ever inspect a Bash command.
    if tool_name == "Bash":
        command = tool_input.get("command", "") or ""
        reason = evaluate_builtins(command, protected, disabled)
        if reason:
            return reason

    # Declarative user rules — evaluated against the tool-appropriate text.
    text = extract_text(tool_name, tool_input)
    return check_user_rules(text, tool_name, config.get("block", []))


def emit_deny(reason: str) -> None:
    """Print the PreToolUse deny object. Deny = JSON on stdout + exit 0."""
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _resolve_project_dir(explicit: Optional[str]) -> str:
    return explicit or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def cmd_validate(project_dir: str) -> int:
    """Fail-loud config check for CI / snap-compact. Non-zero on a bad config."""
    try:
        load_config(project_dir)
    except GuardConfigError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    print("OK: .goodfellow/guards.json is valid (or absent)")
    return 0


def active_guard_set(project_dir: str) -> dict:
    """The set of enforced guards, for the compaction-survival assertion."""
    try:
        config = load_config(project_dir)
        config_error = None
    except GuardConfigError as exc:
        config, config_error = {}, str(exc)
    disabled = set(config.get("disable_builtins", []))
    builtins_off = os.environ.get("GOODFELLOW_GUARDS") == "0"
    return {
        "builtins_enabled": []
        if builtins_off
        else [b for b in BUILTIN_IDS if b not in disabled],
        "protected_branches": config.get(
            "protected_branches", list(DEFAULT_PROTECTED_BRANCHES)
        ),
        "user_rule_ids": [
            r.get("id") for r in config.get("block", []) if isinstance(r, dict)
        ],
        "config_error": config_error,
        "all_disabled": os.environ.get("CLAUDE_HOOK_BYPASS") == "1",
    }


def cmd_selfcheck(project_dir: str) -> int:
    """Print the active guard set as JSON so a post-compaction session can assert
    the BLOCK-rule set is still enforced rather than trusting the summarizer."""
    print(json.dumps(active_guard_set(project_dir), indent=2))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Goodfellow PreToolUse guard engine")
    parser.add_argument(
        "--project-dir",
        default=None,
        help="Project root (defaults to $CLAUDE_PROJECT_DIR or cwd)",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate .goodfellow/guards.json and exit (loud, non-zero on error)",
    )
    parser.add_argument(
        "--selfcheck",
        action="store_true",
        help="Print the active guard set as JSON and exit",
    )
    args = parser.parse_args(argv)
    project_dir = _resolve_project_dir(args.project_dir)

    if args.validate:
        return cmd_validate(project_dir)
    if args.selfcheck:
        return cmd_selfcheck(project_dir)

    # Hook mode: read the PreToolUse payload from stdin.
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        hook_input = json.loads(raw)
    except ValueError:
        return 0  # not our payload; never block on a parse hiccup
    reason = decision_for_input(hook_input, project_dir)
    if reason:
        emit_deny(reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
