"""Tests for the PreToolUse guard engine.

Every deny path is asserted by its JSON output (the deny contract), not by exit
code — a PreToolUse hook denies via permissionDecision JSON on stdout while
exiting 0. `run_hook` drives the real CLI over stdin to prove the wire contract;
the pure-function tests cover the matrix cheaply. Bypass-shape fixtures
(multiline / wrapper / nested-shell / force-refspec) are first-class, per the
"test the parser-equivalent spellings, not just the canonical one" rule.
"""

import json
import os
import subprocess
import sys

import pytest

from guard_engine import (
    BUILTIN_IDS,
    GuardConfigError,
    active_guard_set,
    check_force_push_protected,
    check_git_add_all,
    check_skip_permissions,
    decision_for_input,
    expand_segments,
    load_config,
    validate_config,
)

HERE = os.path.dirname(__file__)
ENGINE = os.path.join(HERE, "guard_engine.py")
HOOKS_JSON = os.path.join(os.path.dirname(HERE), "hooks", "hooks.json")


def run_hook(payload, project_dir, env=None):
    """Invoke the engine as the harness does: JSON on stdin. Returns (rc, parsed_stdout_or_None)."""
    full_env = dict(os.environ)
    for k in ("CLAUDE_HOOK_BYPASS", "GOODFELLOW_GUARDS", "CLAUDE_PROJECT_DIR"):
        full_env.pop(k, None)
    if env:
        full_env.update(env)
    proc = subprocess.run(
        [sys.executable, ENGINE, "--project-dir", str(project_dir)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=full_env,
    )
    out = proc.stdout.strip()
    parsed = json.loads(out) if out else None
    return proc.returncode, parsed


def bash(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def assert_denied(parsed, contains=None):
    assert parsed is not None, "expected a deny JSON object, got no stdout"
    hs = parsed["hookSpecificOutput"]
    assert hs["hookEventName"] == "PreToolUse"
    assert hs["permissionDecision"] == "deny"
    assert hs["permissionDecisionReason"]
    if contains:
        assert contains in hs["permissionDecisionReason"]


# --------------------------------------------------------------------------- #
# Built-in: git add -A / . / --all  (incl. bypass shapes)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "cmd",
    [
        "git add -A",
        "git add .",
        "git add --all",
        "git add -A src/",
        "git -C /repo add --all",
        "cd x && git add .",
        "command git add -A",
        "echo preparing\ngit add -A",  # F1: multiline
        "env git add -A",  # F1: env wrapper
        "FOO=bar git add -A",  # F1: leading assignment
        "sudo git add --all",  # F1: sudo wrapper
        "bash -c 'git add -A'",  # F1: nested shell
        'sh -lc "git add ."',  # F1: nested login shell
    ],
)
def test_git_add_all_denied(cmd):
    assert check_git_add_all(expand_segments(cmd)) is not None


@pytest.mark.parametrize(
    "cmd",
    [
        "git add src/foo.py",
        "git add path/to/file",
        "git status",
        "git commit -m 'add all the things'",  # 'all' in message, not a flag
        "echo git add -A",  # not a git invocation
    ],
)
def test_git_add_specific_allowed(cmd):
    assert check_git_add_all(expand_segments(cmd)) is None


# --------------------------------------------------------------------------- #
# Built-in: --dangerously-skip-permissions  (incl. bypass shapes)
# --------------------------------------------------------------------------- #


def test_skip_perms_denied():
    assert (
        check_skip_permissions(expand_segments("claude --dangerously-skip-permissions"))
        is not None
    )
    assert (
        check_skip_permissions(
            expand_segments("claude --dangerously-skip-permissions=1")
        )
        is not None
    )


def test_skip_perms_nested_shell_denied():
    # F1 class: the flag hidden inside a `bash -c` program is still caught.
    cmd = "bash -c 'claude --dangerously-skip-permissions'"
    assert check_skip_permissions(expand_segments(cmd)) is not None


def test_skip_perms_in_quoted_message_allowed():
    # The caveat: writing the flag text must NOT trip the guard. Inside a quoted
    # commit message the flag is one token (the message), not a standalone arg.
    cmd = "git commit -m 'docs: never use --dangerously-skip-permissions'"
    assert check_skip_permissions(expand_segments(cmd)) is None


def test_skip_perms_not_checked_on_write_content():
    # Built-ins never inspect Write/Edit content, so documenting the flag is safe.
    payload = {
        "tool_name": "Write",
        "tool_input": {"content": "run claude --dangerously-skip-permissions"},
    }
    assert decision_for_input(payload, project_dir="/nonexistent") is None


# --------------------------------------------------------------------------- #
# Built-in: force-push to a protected branch  (incl. bypass shapes)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "cmd",
    [
        "git push --force origin main",
        "git push -f origin master",
        "git push origin main --force",
        "git push --force-with-lease origin main",
        "git push -f origin HEAD:main",
        "git push --force origin +main",
        "git push origin +main",  # F2: + refspec, no flag
        "git push origin +HEAD:refs/heads/main",  # F2: + refspec + full ref
        "git push --force origin HEAD:refs/heads/main",  # F2: full ref normalized
    ],
)
def test_force_push_protected_denied(cmd):
    assert (
        check_force_push_protected(expand_segments(cmd), ["main", "master"]) is not None
    )


@pytest.mark.parametrize(
    "cmd",
    [
        "git push --force origin feature-x",  # feature branch — legitimate
        "git push -f origin my/topic",
        "git push origin main",  # not forced
        "git push",  # bare, no branch named
        "git push -f",  # forced but no branch named -> not blocked
        "git push origin +feature-x",  # + on a non-protected branch
    ],
)
def test_force_push_non_protected_allowed(cmd):
    assert check_force_push_protected(expand_segments(cmd), ["main", "master"]) is None


def test_custom_protected_branches():
    assert (
        check_force_push_protected(
            expand_segments("git push -f origin release"), ["release"]
        )
        is not None
    )
    assert (
        check_force_push_protected(
            expand_segments("git push -f origin main"), ["release"]
        )
        is None
    )


# --------------------------------------------------------------------------- #
# Deny contract over the wire (JSON on stdout, exit 0)
# --------------------------------------------------------------------------- #


def test_wire_deny_git_add_all(tmp_path):
    rc, parsed = run_hook(bash("git add -A"), tmp_path)
    assert rc == 0  # deny is exit 0 + JSON, never a non-zero exit
    assert_denied(parsed, contains="Stage specific files")


def test_wire_deny_git_add_all_multiline(tmp_path):
    rc, parsed = run_hook(bash("echo hi\ngit add -A"), tmp_path)
    assert rc == 0
    assert_denied(parsed, contains="Stage specific files")


def test_wire_deny_skip_perms(tmp_path):
    rc, parsed = run_hook(bash("claude --dangerously-skip-permissions"), tmp_path)
    assert rc == 0
    assert_denied(parsed, contains="permission")


def test_wire_deny_force_push(tmp_path):
    rc, parsed = run_hook(bash("git push --force origin main"), tmp_path)
    assert rc == 0
    assert_denied(parsed, contains="protected branch")


def test_wire_allow_normal_command(tmp_path):
    rc, parsed = run_hook(bash("git status"), tmp_path)
    assert rc == 0
    assert parsed is None  # no stdout == allow


# --------------------------------------------------------------------------- #
# Escape hatches
# --------------------------------------------------------------------------- #


def test_bypass_env_disables_all(tmp_path):
    rc, parsed = run_hook(bash("git add -A"), tmp_path, env={"CLAUDE_HOOK_BYPASS": "1"})
    assert parsed is None


def test_guards_env_zero_disables_builtins(tmp_path):
    rc, parsed = run_hook(bash("git add -A"), tmp_path, env={"GOODFELLOW_GUARDS": "0"})
    assert parsed is None


# --------------------------------------------------------------------------- #
# Declarative user BLOCK rules
# --------------------------------------------------------------------------- #


def write_guards(tmp_path, config):
    gf = tmp_path / ".goodfellow"
    gf.mkdir(exist_ok=True)
    (gf / "guards.json").write_text(json.dumps(config))


def test_user_rule_substring_denied(tmp_path):
    write_guards(
        tmp_path,
        {
            "block": [
                {
                    "id": "no-prod",
                    "pattern": "psql PROD",
                    "reason": "Prod writes need greenlight.",
                }
            ]
        },
    )
    rc, parsed = run_hook(bash("psql PROD -c 'drop table x'"), tmp_path)
    assert_denied(parsed, contains="no-prod")
    assert_denied(parsed, contains="greenlight")


def test_user_rule_regex_denied(tmp_path):
    write_guards(
        tmp_path,
        {
            "block": [
                {
                    "id": "no-rm-rf",
                    "match": "regex",
                    "pattern": r"rm\s+-rf\s+/",
                    "reason": "No rm -rf /.",
                }
            ]
        },
    )
    rc, parsed = run_hook(bash("rm -rf /var/data"), tmp_path)
    assert_denied(parsed, contains="no-rm-rf")


def test_user_rule_respects_tools(tmp_path):
    write_guards(
        tmp_path,
        {
            "block": [
                {
                    "id": "no-secret-file",
                    "pattern": "SECRET_TOKEN",
                    "reason": "No secrets in files.",
                    "tools": ["Write"],
                }
            ]
        },
    )
    # Bash command containing the text is NOT matched (rule targets Write only).
    _, bash_parsed = run_hook(bash("echo SECRET_TOKEN"), tmp_path)
    assert bash_parsed is None
    # Write content IS matched.
    _, write_parsed = run_hook(
        {"tool_name": "Write", "tool_input": {"content": "SECRET_TOKEN=abc"}}, tmp_path
    )
    assert_denied(write_parsed, contains="no-secret-file")


def test_user_rule_bypass_env(tmp_path):
    write_guards(
        tmp_path,
        {
            "block": [
                {
                    "id": "no-prod",
                    "pattern": "psql PROD",
                    "reason": "x",
                    "bypass_env": "PROD_OK",
                }
            ]
        },
    )
    _, parsed = run_hook(bash("psql PROD"), tmp_path, env={"PROD_OK": "1"})
    assert parsed is None


def test_custom_protected_branches_from_config(tmp_path):
    write_guards(tmp_path, {"protected_branches": ["release", "main"]})
    _, parsed = run_hook(bash("git push -f origin release"), tmp_path)
    assert_denied(parsed, contains="protected branch")


def test_disable_builtin_from_config(tmp_path):
    write_guards(tmp_path, {"disable_builtins": ["git-add-all"]})
    _, add_parsed = run_hook(bash("git add -A"), tmp_path)
    assert add_parsed is None  # disabled
    _, push_parsed = run_hook(bash("git push -f origin main"), tmp_path)
    assert_denied(push_parsed)  # other built-ins still active


def test_regex_redos_does_not_hang(tmp_path):
    # F3: a pathological pattern must not wedge the hook. The bounded matcher
    # returns within the timeout (fail-open for that rule) rather than hanging.
    write_guards(
        tmp_path,
        {
            "block": [
                {"id": "redos", "match": "regex", "pattern": "(a+)+$", "reason": "x"}
            ]
        },
    )
    payload = bash("a" * 40 + "!")
    import time

    start = time.time()
    _, parsed = run_hook(payload, tmp_path)
    elapsed = time.time() - start
    assert elapsed < 10, f"hook took {elapsed}s — ReDoS not bounded"


# --------------------------------------------------------------------------- #
# Config validation / failure posture
# --------------------------------------------------------------------------- #


def test_malformed_json_fails_safe_open_but_builtins_hold(tmp_path):
    gf = tmp_path / ".goodfellow"
    gf.mkdir()
    (gf / "guards.json").write_text("{ not valid json ")
    # Live hook must NOT deadlock: a normal command is still allowed...
    _, ok = run_hook(bash("git status"), tmp_path)
    assert ok is None
    # ...an Edit (used to FIX the file) is allowed...
    _, edit_ok = run_hook(
        {"tool_name": "Edit", "tool_input": {"new_string": "whatever"}}, tmp_path
    )
    assert edit_ok is None
    # ...but built-in universal guards still enforce.
    _, add = run_hook(bash("git add -A"), tmp_path)
    assert_denied(add)


def test_validate_cli_fails_loud_on_bad_config(tmp_path):
    gf = tmp_path / ".goodfellow"
    gf.mkdir()
    (gf / "guards.json").write_text("{ broken ")
    proc = subprocess.run(
        [sys.executable, ENGINE, "--project-dir", str(tmp_path), "--validate"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "INVALID" in proc.stderr


def test_validate_cli_ok_when_absent(tmp_path):
    proc = subprocess.run(
        [sys.executable, ENGINE, "--project-dir", str(tmp_path), "--validate"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0


@pytest.mark.parametrize(
    "bad",
    [
        {"block": "not a list"},
        {"block": [{"id": "x", "reason": "y"}]},  # missing pattern
        {
            "block": [{"id": "x", "pattern": "y", "reason": "z", "match": "glob"}]
        },  # bad match type
        {
            "block": [{"id": "x", "pattern": "(", "reason": "z", "match": "regex"}]
        },  # bad regex
        {"protected_branches": "main"},  # not a list
        {"disable_builtins": ["no-such-builtin"]},  # unknown builtin
        {
            "block": [{"id": "x", "pattern": "y", "reason": "z", "flags": 1}]
        },  # F4: non-str flags
        {
            "block": [{"id": "x", "pattern": "y", "reason": "z", "flags": "q"}]
        },  # F4: bad flag char
        {
            "block": [{"id": "x", "pattern": "y", "reason": "z", "bypass_env": 1}]
        },  # F4: non-str env
        {
            "block": [{"id": "x", "pattern": "y", "reason": "z", "bypass_env": "1BAD"}]
        },  # F4: bad env name
        {
            "block": [  # F4: duplicate ids
                {"id": "dup", "pattern": "a", "reason": "z"},
                {"id": "dup", "pattern": "b", "reason": "z"},
            ]
        },
    ],
)
def test_validate_config_rejects(bad):
    with pytest.raises(GuardConfigError):
        validate_config(bad)


def test_validate_config_accepts_typed_optionals():
    validate_config(
        {
            "block": [
                {
                    "id": "ok",
                    "pattern": "p",
                    "reason": "r",
                    "match": "regex",
                    "flags": "im",
                    "bypass_env": "MY_OK",
                    "tools": ["Bash", "Write"],
                }
            ]
        }
    )


# --------------------------------------------------------------------------- #
# Compaction-survival self-check (full-rule digests, not id proxy)
# --------------------------------------------------------------------------- #


def test_selfcheck_lists_active_guard_set(tmp_path):
    write_guards(
        tmp_path, {"block": [{"id": "no-prod", "pattern": "PROD", "reason": "x"}]}
    )
    proc = subprocess.run(
        [sys.executable, ENGINE, "--project-dir", str(tmp_path), "--selfcheck"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    state = json.loads(proc.stdout)
    assert set(state["builtins_enabled"]) == set(BUILTIN_IDS)
    assert [r["id"] for r in state["user_rules"]] == ["no-prod"]
    assert state["config_error"] is None


def test_selfcheck_digest_changes_when_pattern_changes(tmp_path):
    # F5: same id, changed pattern -> digest changes, so drift is detected.
    write_guards(tmp_path, {"block": [{"id": "r", "pattern": "A", "reason": "x"}]})
    before = active_guard_set(str(tmp_path))["user_rules"][0]["digest"]
    write_guards(tmp_path, {"block": [{"id": "r", "pattern": "B", "reason": "x"}]})
    after = active_guard_set(str(tmp_path))["user_rules"][0]["digest"]
    assert before != after


def test_assert_guard_set_detects_drift(tmp_path):
    # F5: --assert-guard-set exits non-zero when the set changes, not just warns.
    write_guards(tmp_path, {"block": [{"id": "r", "pattern": "A", "reason": "x"}]})
    baseline = tmp_path / "baseline.json"
    proc = subprocess.run(
        [sys.executable, ENGINE, "--project-dir", str(tmp_path), "--selfcheck"],
        capture_output=True,
        text=True,
    )
    baseline.write_text(proc.stdout)
    # No change -> exit 0
    ok = subprocess.run(
        [
            sys.executable,
            ENGINE,
            "--project-dir",
            str(tmp_path),
            "--assert-guard-set",
            str(baseline),
        ],
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0
    # Change the pattern -> drift -> exit 1
    write_guards(tmp_path, {"block": [{"id": "r", "pattern": "B", "reason": "x"}]})
    drift = subprocess.run(
        [
            sys.executable,
            ENGINE,
            "--project-dir",
            str(tmp_path),
            "--assert-guard-set",
            str(baseline),
        ],
        capture_output=True,
        text=True,
    )
    assert drift.returncode == 1
    assert "DRIFT" in drift.stderr


def test_active_guard_set_surfaces_config_error(tmp_path):
    gf = tmp_path / ".goodfellow"
    gf.mkdir()
    (gf / "guards.json").write_text("{ broken ")
    state = active_guard_set(str(tmp_path))
    assert state["config_error"] is not None
    # built-ins still reported as enforced even when user config is broken
    assert set(state["builtins_enabled"]) == set(BUILTIN_IDS)


def test_load_config_absent_is_empty(tmp_path):
    assert load_config(str(tmp_path)) == {}


# --------------------------------------------------------------------------- #
# F6: pin the host wiring (guard is dead if hooks.json stops calling it)
# --------------------------------------------------------------------------- #


def test_hooks_json_wires_guard_engine():
    with open(HOOKS_JSON, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    pre = data["hooks"]["PreToolUse"]
    entries = [
        e
        for e in pre
        if any("guard_engine.py" in h.get("command", "") for h in e.get("hooks", []))
    ]
    assert entries, "no PreToolUse hook invokes guard_engine.py"
    entry = entries[0]
    assert "Bash" in entry["matcher"]
    # the SessionStart recall hook must survive alongside the new PreToolUse one
    assert data["hooks"]["SessionStart"]


def test_hook_registered_selfcheck(tmp_path):
    # active_guard_set reports the wiring as present when CLAUDE_PLUGIN_ROOT points
    # at the repo (best-effort; None when the env is unset).
    plugin_root = os.path.dirname(HERE)
    state = active_guard_set(str(tmp_path))
    assert state["hook_registered"] is None  # env unset in-process
    proc = subprocess.run(
        [sys.executable, ENGINE, "--project-dir", str(tmp_path), "--selfcheck"],
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PLUGIN_ROOT": plugin_root},
    )
    assert json.loads(proc.stdout)["hook_registered"] is True
