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

## Bypass-shape coverage (why parsing, not grep)

A guard that only recognizes the canonical spelling of a dangerous command is a
guard an attacker (or a careless paste) walks around. So the built-ins parse
shell structure rather than substring-match:

  - Commands are split on unquoted newlines and `;`/`|`/`&`/`()` control
    operators, so `echo hi\ngit add -A` is seen as two commands, not one.
  - Leading env assignments and wrappers (`env`, `sudo`, `command`, `FOO=bar …`)
    are stripped before the subcommand is read, so `env git add -A` is caught.
  - Nested `sh -c '…'` / `bash -c '…'` programs are recursively expanded
    (bounded depth), so `bash -c 'git add -A'` is caught.
  - Force-push detection understands `+refspec` force syntax and normalizes
    `refs/heads/main` to `main`.

Conversely, matching is shlex-token based, so merely *writing* a blocked flag as
text (`git commit -m "docs: --dangerously-skip-permissions"`) is one token — the
message — and never trips a guard, and built-ins inspect only the `Bash` tool's
command, never Write/Edit content.

Known, deliberate limitation: a bare `git push --force` with no refspec is NOT
blocked (the target branch cannot be resolved statically, and force-pushing a
feature branch is routine). Name the protected branch to be protected.

## Untrusted config (ReDoS bound)

`.goodfellow/guards.json` is repository-supplied and therefore untrusted input.
A user `regex` rule runs on the text of every tool call, so a pathological
pattern (`(a+)+$`) could catastrophically backtrack and wedge the session — the
PreToolUse hook would hang before Bash/Write/Edit. Regex matching is bounded by a
wall-clock timeout (POSIX) and a payload-length cap; on timeout the rule is
skipped with a loud stderr warning rather than hanging (fail-open for that one
rule, so the session is never wedged — the primary harm P61 warns about).

## Failure posture (fail-safe-open for the live hook, fail-loud on demand)

A governance gate must not *itself* block legitimate work. If `.goodfellow/guards.json`
is malformed, the live hook skips the user rules (built-ins still enforce) and
writes a warning to stderr — it never deny-alls the session into a deadlock where
you cannot even edit the file to fix it. For a loud, CI/snap-compact-style check,
run `guard_engine.py --validate`, which exits non-zero on a bad config;
`guard_engine.py --selfcheck`, which prints the active guard set (with full
per-rule digests) so a post-compaction session can assert governance survived the
boundary; and `guard_engine.py --assert-guard-set <baseline.json>`, which exits
non-zero if the enforced set drifted from a snapshot.

Escape hatches: `CLAUDE_HOOK_BYPASS=1` (all guards off), `GOODFELLOW_GUARDS=0`
(built-ins off), or a per-rule `bypass_env` in guards.json.
"""

from __future__ import annotations

import hashlib
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

# Leading wrappers/assignments to strip before reading a subcommand.
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WRAPPERS = {
    "command",
    "env",
    "sudo",
    "nice",
    "nohup",
    "stdbuf",
    "setsid",
    "time",
    "builtin",
    "exec",
}
_SHELLS = {"sh", "bash", "zsh", "dash", "ash", "ksh"}
_MAX_NEST_DEPTH = 4
_REGEX_TIMEOUT_S = 1.0
_REGEX_MAX_LEN = 100_000
_ALLOWED_REGEX_FLAGS = set("ims")


class GuardConfigError(Exception):
    """Raised when .goodfellow/guards.json is present but unusable (fail-loud path)."""


# --------------------------------------------------------------------------- #
# Command tokenization (shared by every built-in guard)
# --------------------------------------------------------------------------- #

_CONTROL_TOKENS = {"&&", "||", ";", "&", "|", "(", ")"}


def _split_unquoted_newlines(command: str) -> List[str]:
    """Split a command on newlines that are NOT inside quotes, honoring backslash
    line-continuation (`\\<newline>` joins). Quoted newlines stay intact."""
    parts: List[str] = []
    buf: List[str] = []
    quote: Optional[str] = None
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if quote is not None:
            buf.append(c)
            if c == quote:
                quote = None
            elif c == "\\" and quote == '"' and i + 1 < n:
                buf.append(command[i + 1])
                i += 2
                continue
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            buf.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            nxt = command[i + 1]
            if nxt == "\n":
                i += 2  # line continuation: drop the backslash-newline
                continue
            buf.append(c)
            buf.append(nxt)
            i += 2
            continue
        if c == "\n":
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    parts.append("".join(buf))
    return [p for p in parts if p.strip()]


def _split_control(command: str) -> List[List[str]]:
    """shlex-split one command line into argument segments at control operators."""
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


def split_segments(command: str) -> List[List[str]]:
    """Split a shell command into argument segments.

    Splits first on unquoted newlines, then on `;`/`|`/`&`/`()` control
    operators. `git add -A && rm x` and `echo hi\\ngit add -A` both yield two
    segments; text inside quotes stays one token.
    """
    segments: List[List[str]] = []
    for line in _split_unquoted_newlines(command):
        segments.extend(_split_control(line))
    return segments


def _basename_is(token: str, names: set) -> bool:
    """True when token is a bare name in `names`, or a path whose basename is."""
    lowered = {n.lower() for n in names}
    if token in names:
        return True
    if token.startswith("/") or token.startswith("./") or token.startswith("../"):
        return PurePosixPath(token).name in names
    if re.match(r"^[A-Za-z]:[\\/]", token) or "\\" in token:
        return PureWindowsPath(token).name.lower() in lowered
    return False


def _strip_wrappers(tokens: List[str]) -> List[str]:
    """Drop leading `command`, env assignments, and known wrappers so the real
    subcommand is first. `env FOO=1 sudo git add -A` -> `git add -A`."""
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if _ENV_ASSIGN.match(t) or t in _WRAPPERS:
            i += 1
            continue
        break
    return tokens[i:]


def _git_arg_start(tokens: List[str]) -> Optional[int]:
    """Index (into the ORIGINAL list) of the git *subcommand*, after stripping
    wrappers and git's own top-level options (`-C <dir>`, `--git-dir=…`,
    `--work-tree=…`). None if the segment is not a git invocation."""
    stripped = _strip_wrappers(tokens)
    offset = len(tokens) - len(stripped)
    if not stripped or not _basename_is(stripped[0], {"git", "git.exe"}):
        return None
    index = 1
    while index < len(stripped):
        token = stripped[index]
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
    return offset + index


def _nested_shell_program(tokens: List[str]) -> Optional[str]:
    """If the segment is `sh -c PROG` / `bash -lc PROG` (after wrappers), return
    PROG so it can be recursively parsed. None otherwise."""
    toks = _strip_wrappers(tokens)
    if not toks or not _basename_is(toks[0], _SHELLS):
        return None
    j = 1
    while j < len(toks):
        t = toks[j]
        if t == "-c" or (
            t.startswith("-")
            and t != "--"
            and "c" in t
            and all(ch in "ilrsxc-" for ch in t)
        ):
            return toks[j + 1] if j + 1 < len(toks) else None
        if not t.startswith("-"):
            break
        j += 1
    return None


def expand_segments(command: str, _depth: int = 0) -> List[List[str]]:
    """All command segments, including those nested inside `sh -c '…'` programs
    (recursively, bounded by `_MAX_NEST_DEPTH`). Raises ValueError on unparseable
    shell (unbalanced quotes) — callers decide whether to block or pass."""
    segments = split_segments(command)
    if _depth >= _MAX_NEST_DEPTH:
        return segments
    out = list(segments)
    for tokens in segments:
        prog = _nested_shell_program(tokens)
        if prog:
            try:
                out.extend(expand_segments(prog, _depth + 1))
            except ValueError:
                pass  # a nested program we cannot parse: don't guess
    return out


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


def _normalize_ref(refspec: str) -> str:
    """Reduce a push refspec to its short destination branch name.
    `+HEAD:refs/heads/main` -> `main`; `origin` -> `origin`."""
    ref = refspec.lstrip("+").split(":")[-1]
    for prefix in ("refs/heads/", "heads/"):
        if ref.startswith(prefix):
            ref = ref[len(prefix) :]
            break
    return ref


def check_force_push_protected(
    segments: Sequence[List[str]], protected: Sequence[str]
) -> Optional[str]:
    """Deny a force-push whose refspec resolves to a protected branch.

    Understands both a global force flag (`--force`/`-f`/`--force-with-lease`) and
    per-refspec `+` force syntax, and normalizes `refs/heads/<b>` to `<b>`. A bare
    `git push --force` with no refspec is deliberately NOT blocked (unresolvable
    target; feature-branch force-push is routine)."""
    protected_set = {_normalize_ref(b) for b in protected if b}
    for tokens in segments:
        start = _git_arg_start(tokens)
        if start is None or start >= len(tokens) or tokens[start] != "push":
            continue
        args = tokens[start + 1 :]
        global_force = any(
            a in {"-f", "--force"}
            or a == "--force-with-lease"
            or a.startswith("--force-with-lease=")
            or a.startswith("--force-if-includes")
            for a in args
        )
        refspecs = [a for a in args if not a.startswith("-")]
        for a in refspecs:
            if not (global_force or a.startswith("+")):
                continue
            if _normalize_ref(a) in protected_set:
                return (
                    f"Blocked force-push to protected branch "
                    f"'{_normalize_ref(a)}'. Force-pushing "
                    f"{'/'.join(sorted(protected_set))} rewrites shared history and "
                    "can destroy other people's commits. Push a feature branch and "
                    "open a PR, or drop the force."
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
    """Fail loud on a structurally invalid config so a typo never silently disarms
    a rule — and so `--validate` catches every field the live hook consumes."""
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
    seen_ids = set()
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise GuardConfigError(f"{path}: block[{i}] must be an object")
        for req in ("id", "pattern", "reason"):
            if not isinstance(rule.get(req), str) or not rule.get(req):
                raise GuardConfigError(
                    f"{path}: block[{i}] missing required string '{req}'"
                )
        rid = rule["id"]
        if rid in seen_ids:
            raise GuardConfigError(f"{path}: duplicate rule id '{rid}'")
        seen_ids.add(rid)
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
        # Optional fields consumed at runtime must be typed here or the live hook
        # crashes instead of taking its documented malformed-config path.
        flags = rule.get("flags", "")
        if not isinstance(flags, str) or any(
            ch not in _ALLOWED_REGEX_FLAGS for ch in flags
        ):
            raise GuardConfigError(
                f"{path}: block[{i}].flags must be a string of {sorted(_ALLOWED_REGEX_FLAGS)}"
            )
        bypass_env = rule.get("bypass_env")
        if bypass_env is not None and (
            not isinstance(bypass_env, str) or not _ENV_NAME.match(bypass_env)
        ):
            raise GuardConfigError(
                f"{path}: block[{i}].bypass_env must be a valid env-var name"
            )
        tools = rule.get("tools", ["Bash"])
        if not isinstance(tools, list) or not all(
            isinstance(t, str) and t for t in tools
        ):
            raise GuardConfigError(
                f"{path}: block[{i}].tools must be a list of tool names"
            )


def _regex_flags(flags: str) -> int:
    out = 0
    if "i" in flags:
        out |= re.IGNORECASE
    if "m" in flags:
        out |= re.MULTILINE
    if "s" in flags:
        out |= re.DOTALL
    return out


def _bounded_regex_search(pattern: str, text: str, flags: int, rule_id: str) -> bool:
    """Run an untrusted regex with a wall-clock bound so a pathological pattern
    cannot wedge the hook (P61). On POSIX main-thread, a SIGALRM timeout skips the
    rule (fail-open) with a loud warning; elsewhere only the length cap applies."""
    if len(text) > _REGEX_MAX_LEN:
        text = text[:_REGEX_MAX_LEN]
    compiled = re.compile(pattern, flags)
    try:
        import signal

        has_alarm = hasattr(signal, "SIGALRM")
    except Exception:
        has_alarm = False
    if not has_alarm:
        return compiled.search(text) is not None

    class _Timeout(Exception):
        pass

    def _handler(signum, frame):
        raise _Timeout()

    try:
        old = signal.signal(signal.SIGALRM, _handler)
    except (ValueError, OSError):
        return compiled.search(text) is not None  # not main thread
    signal.setitimer(signal.ITIMER_REAL, _REGEX_TIMEOUT_S)
    try:
        return compiled.search(text) is not None
    except _Timeout:
        print(
            f"goodfellow guard_engine: rule '{rule_id}' regex exceeded "
            f"{_REGEX_TIMEOUT_S}s and was skipped — simplify the pattern.",
            file=sys.stderr,
        )
        return False
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


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
        if match == "substring":
            hit = pattern in text
        else:
            hit = _bounded_regex_search(
                pattern, text, _regex_flags(rule.get("flags", "")), rule.get("id", "?")
            )
        if hit:
            return f"Blocked by goodfellow guard '{rule['id']}': {rule['reason']}"
    return None


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
        segments = expand_segments(command)
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
# Self-check / drift assertion (compaction-survival)
# --------------------------------------------------------------------------- #


def _rule_digest(rule: dict) -> str:
    """A stable digest of a rule's *effective* content, so a changed pattern/tools/
    flags/bypass is detected even when the id is unchanged (not an id-only proxy)."""
    effective = {
        "id": rule.get("id"),
        "pattern": rule.get("pattern"),
        "reason": rule.get("reason"),
        "match": rule.get("match", "substring"),
        "flags": rule.get("flags", ""),
        "tools": rule.get("tools", ["Bash"]),
        "bypass_env": rule.get("bypass_env"),
    }
    blob = json.dumps(effective, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _hook_registered() -> Optional[bool]:
    """Best-effort: is the PreToolUse hook still wired to this engine? Reads the
    plugin's hooks.json when CLAUDE_PLUGIN_ROOT is set. None = could not tell."""
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not root:
        return None
    path = os.path.join(root, "hooks", "hooks.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    for entry in data.get("hooks", {}).get("PreToolUse", []):
        for hook in entry.get("hooks", []):
            if "guard_engine" in hook.get("command", ""):
                return True
    return False


def active_guard_set(project_dir: str) -> dict:
    """The set of enforced guards, for the compaction-survival assertion. Includes
    full per-rule digests (not just ids) so a semantic change is caught."""
    try:
        config = load_config(project_dir)
        config_error = None
    except GuardConfigError as exc:
        config, config_error = {}, str(exc)
    disabled = set(config.get("disable_builtins", []))
    builtins_off = os.environ.get("GOODFELLOW_GUARDS") == "0"
    rules = [r for r in config.get("block", []) if isinstance(r, dict)]
    return {
        "builtins_enabled": []
        if builtins_off
        else [b for b in BUILTIN_IDS if b not in disabled],
        "protected_branches": config.get(
            "protected_branches", list(DEFAULT_PROTECTED_BRANCHES)
        ),
        "user_rules": [{"id": r.get("id"), "digest": _rule_digest(r)} for r in rules],
        "hook_registered": _hook_registered(),
        "config_error": config_error,
        "all_disabled": os.environ.get("CLAUDE_HOOK_BYPASS") == "1",
    }


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


def cmd_selfcheck(project_dir: str) -> int:
    """Print the active guard set as JSON so a post-compaction session can assert
    the BLOCK-rule set is still enforced rather than trusting the summarizer."""
    print(json.dumps(active_guard_set(project_dir), indent=2, sort_keys=True))
    return 0


def cmd_assert_guard_set(project_dir: str, baseline_path: str) -> int:
    """Compare the current guard set to a baseline snapshot; non-zero on drift."""
    current = active_guard_set(project_dir)
    try:
        with open(baseline_path, "r", encoding="utf-8") as fh:
            baseline = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"DRIFT: cannot read baseline {baseline_path}: {exc}", file=sys.stderr)
        return 1
    if current.get("config_error"):
        print(f"DRIFT: config error: {current['config_error']}", file=sys.stderr)
        return 1
    if current != baseline:
        print(
            "DRIFT: guard set changed across the boundary — investigate before "
            "proceeding.",
            file=sys.stderr,
        )
        return 1
    print("OK: guard set intact.")
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
    parser.add_argument(
        "--assert-guard-set",
        metavar="BASELINE",
        default=None,
        help="Exit non-zero if the guard set drifted from BASELINE snapshot",
    )
    args = parser.parse_args(argv)
    project_dir = _resolve_project_dir(args.project_dir)

    if args.validate:
        return cmd_validate(project_dir)
    if args.selfcheck:
        return cmd_selfcheck(project_dir)
    if args.assert_guard_set:
        return cmd_assert_guard_set(project_dir, args.assert_guard_set)

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
