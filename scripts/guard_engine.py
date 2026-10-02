#!/usr/bin/env python3
"""Goodfellow PreToolUse guard engine: asks before the few hard-to-reverse actions.

Models are capable, so goodfellow keeps its tool-layer checks few, and none of its
own checks blocks: they return `permissionDecision: "ask"` with a one-line reason,
Claude Code shows its normal confirmation, and the user's yes goes through. This
engine backs a single PreToolUse hook (see hooks/hooks.json) and evaluates:

  1. One built-in: the `--dangerously-skip-permissions` CLI flag, which turns off the
     permission prompt for every tool call (asks).
  2. The stop list (stop_list.py): force-push, a push to the default branch, a
     tag push or release, a package publish (asks).
  3. Declarative user BLOCK rules from `.goodfellow/guards.json`. These deny,
     because the project wrote them.

## The decision contract (assert JSON, not exit code)

A PreToolUse hook decides by printing a permissionDecision JSON object ("ask" or
"deny") on stdout and exiting 0. A non-zero exit is a *hook error*. Tests assert
the JSON, never the exit code. See `emit` / `decide`.

## Parsing, not grep

Commands are split on unquoted newlines and `;`/`|`/`&`/`()` control operators;
leading env assignments and wrappers (`env`, `sudo`, `command`) are stripped; nested
`sh -c '...'` programs are expanded (bounded depth). Matching is shlex-token based,
so merely writing a flag as text inside a quoted message never trips a check, and
the built-ins inspect only the `Bash` tool's command, never Write/Edit content.

## Untrusted config (ReDoS bound)

`.goodfellow/guards.json` is repository-supplied and therefore untrusted input.
A user `regex` rule runs on the text of every tool call, so a pathological
pattern (`(a+)+$`) could catastrophically backtrack and wedge the session — the
PreToolUse hook would hang before Bash/Write/Edit. When any regex rule applies,
ALL applicable rules are evaluated in a child process under ONE hard wall-clock
budget (`subprocess.run(timeout=...)`), which is platform-independent (works where
`signal` is unavailable), covers every rule at once (not one timer per rule), and
sees the FULL payload (no length-cap that a suffix could hide behind). A budget
overrun is a *conservative deny*: an operation that cannot be evaluated is
blocked, never silently allowed. Pure-substring configs skip the subprocess and
match inline (linear, safe).

## Failure posture (fail-safe-open for the live hook, fail-loud on demand)

A governance gate must not *itself* block legitimate work. If `.goodfellow/guards.json`
is malformed, the live hook skips the user rules (built-ins still enforce) and
writes a warning to stderr: it never deny-alls the session into a deadlock where
you cannot even edit the file to fix it. For a loud, CI/snap-compact-style check,
run `guard_engine.py --validate`, which exits non-zero on a bad config;
`guard_engine.py --selfcheck`, which prints the active guard set (with full
per-rule digests) so a post-compaction session can assert governance survived the
boundary; and `guard_engine.py --assert-guard-set <baseline.json>`, which exits
non-zero if the enforced set drifted from a snapshot.

Switches: `GOODFELLOW_STOP_LIST=0` (the built-in asks off), `CLAUDE_HOOK_BYPASS=1`
(everything off), or a per-rule `bypass_env` in guards.json.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
from pathlib import PurePosixPath, PureWindowsPath
from typing import Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stop_list  # noqa: E402

DEFAULT_PROTECTED_BRANCHES = ("main", "master")
BUILTIN_IDS = ("dangerous-skip-permissions",)
# Built-ins removed in 0.4.1, accepted in old configs and ignored.
RETIRED_BUILTIN_IDS = ("git-add-all", "force-push-protected")
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
# git's own options (before the subcommand) that take the NEXT token as their value.
_GIT_VALUE_OPTS = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--config-env",
    "--super-prefix",
    "--list-cmds",
    "--attr-source",
}
_MAX_NEST_DEPTH = 4
_REGEX_BUDGET_S = 2.0  # one hard wall-clock budget for ALL regex rules combined
_ALLOWED_REGEX_FLAGS = set("ims")
# `git push` options that consume a following value token (so it is not a refspec).
_PUSH_VALUE_OPTS = {
    "--repo",
    "-o",
    "--push-option",
    "--receive-pack",
    "--exec",
    "--recurse-submodules",
}


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
    # Windows first: a drive path (`C:/Git/bin/GIT.EXE`, `C:\\...`) or any
    # backslash path compares case-insensitively, whichever slash it uses.
    if re.match(r"^[A-Za-z]:[\\/]", token) or "\\" in token:
        return PureWindowsPath(token).name.lower() in lowered
    if "/" in token:
        # Any path runs the binary it names: `/usr/bin/git`, `./git`, and a
        # relative `usr/bin/git` alike.
        return PurePosixPath(token).name in names
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
        if token in _GIT_VALUE_OPTS:
            index += 2  # the option and its value
            continue
        if token.startswith("-"):
            # `-C<dir>`, `--git-dir=…`, `--config-env=…`, and boolean globals such
            # as `--no-pager`, `-p` or `--bare`: none of them is the subcommand.
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


def check_skip_permissions(segments: Sequence[List[str]]) -> Optional[str]:
    for tokens in segments:
        for token in tokens:
            if token == SKIP_PERMS_FLAG or token.startswith(SKIP_PERMS_FLAG + "="):
                return (
                    f"goodfellow (dangerous-skip-permissions): `{SKIP_PERMS_FLAG}` turns "
                    "off the permission prompt for every tool call. Confirm to go ahead."
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
    known = BUILTIN_IDS + stop_list.STOP_IDS
    retired = RETIRED_BUILTIN_IDS + stop_list.RETIRED_IDS
    for bad in [b for b in disabled if b not in known + retired]:
        raise GuardConfigError(
            f"{path}: unknown built-in '{bad}' in 'disable_builtins' "
            f"(known: {', '.join(known)})"
        )
    stops = data.get("stop_list", {})
    if not isinstance(stops, dict):
        raise GuardConfigError(f"{path}: 'stop_list' must be an object")
    for key in ("publish_commands",) + stop_list.RETIRED_KEYS:
        value = stops.get(key)
        if value is not None and (
            not isinstance(value, list)
            or not all(isinstance(v, str) and v.strip() for v in value)
        ):
            raise GuardConfigError(
                f"{path}: 'stop_list.{key}' must be a list of non-empty strings"
            )
    for key in stops:
        if key not in ("publish_commands",) + stop_list.RETIRED_KEYS:
            raise GuardConfigError(f"{path}: unknown key 'stop_list.{key}'")
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


def _rule_hit(rule: dict, text: str) -> bool:
    """Does one rule match the FULL text? substring (linear) or regex (backtracking).
    Regex is only ever called from inside the bounded worker below."""
    if rule.get("match", "substring") == "substring":
        return rule["pattern"] in text
    return (
        re.search(rule["pattern"], text, _regex_flags(rule.get("flags", "")))
        is not None
    )


def _first_match_index(rules: Sequence[dict], text: str) -> Optional[int]:
    for i, rule in enumerate(rules):
        if _rule_hit(rule, text):
            return i
    return None


def _regex_worker() -> int:
    """Hidden CLI mode (`--regex-worker`): evaluate rules against text read from
    stdin, print the index of the first match (or nothing), exit 0. Run as a child
    process so the PARENT's subprocess timeout is a hard, platform-independent
    wall-clock bound over ALL rules and the WHOLE payload at once — no truncation,
    one aggregate budget (P61)."""
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 2
    idx = _first_match_index(data.get("rules", []), data.get("text", ""))
    if idx is not None:
        print(idx)
    return 0


def check_user_rules(text: str, tool_name: str, rules: Iterable[dict]) -> Optional[str]:
    """Evaluate declarative BLOCK rules (in config order) against this tool's text.

    Rules that don't apply to this tool, or whose `bypass_env` is set, are dropped
    first. If none of the applicable rules use `regex`, matching is linear and runs
    inline (the common fast path). If any regex rule applies, ALL applicable rules
    are evaluated inside a child worker under one hard wall-clock budget, so a
    catastrophic-backtracking pattern in the (untrusted) repo config can neither
    wedge the hook nor be dodged by putting the dangerous text past a length cap.
    A budget overrun is a *conservative deny* — an operation that cannot be
    evaluated is blocked, not silently allowed."""
    applicable = [
        r
        for r in rules
        if tool_name in r.get("tools", ["Bash"])
        and not (r.get("bypass_env") and os.environ.get(r["bypass_env"]) == "1")
    ]
    if not applicable:
        return None

    def _reason(rule: dict) -> str:
        return f"Blocked by goodfellow guard '{rule['id']}': {rule['reason']}"

    if not any(r.get("match", "substring") == "regex" for r in applicable):
        idx = _first_match_index(applicable, text)
        return _reason(applicable[idx]) if idx is not None else None

    payload = json.dumps(
        {
            "text": text,
            "rules": [
                {
                    "id": r["id"],
                    "reason": r["reason"],
                    "pattern": r["pattern"],
                    "match": r.get("match", "substring"),
                    "flags": r.get("flags", ""),
                }
                for r in applicable
            ],
        }
    )
    try:
        import subprocess

        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--regex-worker"],
            input=payload,
            capture_output=True,
            text=True,
            timeout=_REGEX_BUDGET_S,
        )
    except subprocess.TimeoutExpired:
        print(
            "goodfellow guard_engine: guards.json regex evaluation exceeded "
            f"{_REGEX_BUDGET_S}s — blocking conservatively.",
            file=sys.stderr,
        )
        return (
            "Blocked: a `regex` rule in .goodfellow/guards.json could not be "
            f"evaluated within {_REGEX_BUDGET_S}s (possible catastrophic "
            "backtracking). Blocking conservatively — simplify the pattern(s). "
            "Run `guard_engine.py --validate` to locate them."
        )
    except OSError as exc:
        print(
            f"goodfellow guard_engine: regex worker failed to launch: {exc} "
            "(regex rules skipped)",
            file=sys.stderr,
        )
        return None  # engine failure -> fail-open rather than a spurious block
    if proc.returncode != 0:
        print(
            f"goodfellow guard_engine: regex worker error rc={proc.returncode} "
            "(regex rules skipped)",
            file=sys.stderr,
        )
        return None
    out = proc.stdout.strip()
    if not out:
        return None
    try:
        return _reason(applicable[int(out)])
    except (ValueError, IndexError):
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
    """The built-in check over a Bash command string: an ask reason, or None.
    `GOODFELLOW_STOP_LIST=0` turns it off along with the stop list."""
    if os.environ.get("GOODFELLOW_STOP_LIST") == "0":
        return None
    if "dangerous-skip-permissions" in disabled:
        return None
    try:
        segments = expand_segments(command)
    except ValueError:
        # Unparseable shell (unbalanced quotes): don't guess.
        return None
    return check_skip_permissions(segments)


def evaluate_stop_list(
    command: str, cwd: str, config: dict, project_dir: str
) -> Optional[str]:
    """The stop list (see stop_list.py): an ask reason, or None. On in every
    mode; `GOODFELLOW_STOP_LIST=0` turns it off."""
    if os.environ.get("GOODFELLOW_STOP_LIST") == "0":
        return None
    try:
        segments = expand_segments(command)
    except ValueError:
        return None
    return stop_list.evaluate(
        segments, cwd, config, project_dir, config.get("disable_builtins", [])
    )


def decide(
    hook_input: dict, project_dir: str, config: Optional[dict] = None
) -> Optional[Tuple[str, str]]:
    """(decision, reason) for a PreToolUse payload, or None to allow.

    Built-ins and the stop list never block: they return "ask", so Claude Code
    shows its normal confirmation and the user's yes goes through. Only the
    project's own `block` rules in .goodfellow/guards.json deny, because the user
    wrote them. Pure (no stdout/exit): the unit under test. Fail-safe-open on a
    malformed user config: built-ins still run, user rules are skipped.
    """
    if os.environ.get("CLAUDE_HOOK_BYPASS") == "1":
        return None
    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {}) or {}

    if config is None:
        try:
            config = load_config(project_dir)
        except GuardConfigError as exc:
            print(
                f"goodfellow guard_engine: {exc} (user rules skipped)", file=sys.stderr
            )
            config = {}

    protected = config.get("protected_branches", list(DEFAULT_PROTECTED_BRANCHES))
    disabled = config.get("disable_builtins", [])

    if tool_name == "Bash":
        command = tool_input.get("command", "") or ""
        reason = evaluate_builtins(command, protected, disabled)
        if reason:
            return "ask", reason
        reason = evaluate_stop_list(
            command, hook_input.get("cwd") or project_dir, config, project_dir
        )
        if reason:
            return "ask", reason

    text = extract_text(tool_name, tool_input)
    reason = check_user_rules(text, tool_name, config.get("block", []))
    return ("deny", reason) if reason else None


def decision_for_input(hook_input: dict, project_dir: str) -> Optional[str]:
    """The reason of `decide` (ask or deny), or None."""
    d = decide(hook_input, project_dir)
    return d[1] if d else None


def emit(decision: str, reason: str) -> None:
    """Print the PreToolUse decision object ("ask" or "deny"): JSON on stdout, exit 0."""
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision,
                    "permissionDecisionReason": " ".join(reason.split()),
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
    builtins_off = os.environ.get("GOODFELLOW_STOP_LIST") == "0"
    rules = [r for r in config.get("block", []) if isinstance(r, dict)]
    return {
        "builtins_enabled": []
        if builtins_off
        else [b for b in BUILTIN_IDS if b not in disabled],
        "stop_list_enabled": []
        if builtins_off
        else [b for b in stop_list.STOP_IDS if b not in disabled],
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


_NESTED_QUANTIFIER = re.compile(r"\([^()]*[+*?][^()]*\)\s*[+*]")


def _redos_prone(pattern: str) -> bool:
    """Best-effort heuristic: a quantifier applied to a group that itself contains
    a quantifier (`(a+)+`, `(a*)+`, `(.+)*`) — the classic catastrophic-backtracking
    shape. Not exhaustive, but enough to point --validate at a likely offender."""
    return _NESTED_QUANTIFIER.search(pattern) is not None


def cmd_validate(project_dir: str) -> int:
    """Fail-loud config check for CI / snap-compact. Non-zero on a bad config.

    Also a genuine ReDoS diagnostic: names each regex rule whose pattern looks
    catastrophic-backtracking-prone, so the runtime timeout message ("run
    --validate to locate them") actually leads somewhere. Risk is a warning, not
    an error — a complex pattern may be intentional — so the exit stays 0."""
    try:
        config = load_config(project_dir)
    except GuardConfigError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    risky = [
        r["id"]
        for r in config.get("block", [])
        if isinstance(r, dict)
        and r.get("match") == "regex"
        and isinstance(r.get("pattern"), str)
        and _redos_prone(r["pattern"])
    ]
    if risky:
        print(
            "WARNING: possible catastrophic-backtracking regex in rule(s): "
            f"{', '.join(risky)} — a nested quantifier like `(a+)+` can hang the "
            "guard on a crafted payload (it is then conservatively denied). "
            "Rewrite the pattern to a linear form.",
            file=sys.stderr,
        )
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
    parser.add_argument(
        "--regex-worker",
        action="store_true",
        help=argparse.SUPPRESS,  # internal: bounded regex evaluation child process
    )
    args = parser.parse_args(argv)

    if args.regex_worker:
        return _regex_worker()

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
    d = decide(hook_input, project_dir)
    if d:
        emit(*d)
    return 0


if __name__ == "__main__":
    sys.exit(main())
