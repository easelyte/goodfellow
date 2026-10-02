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
    check_skip_permissions,
    decide,
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
    for k in ("CLAUDE_HOOK_BYPASS", "GOODFELLOW_STOP_LIST", "CLAUDE_PROJECT_DIR"):
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
    # the flag hidden inside a `bash -c` program is still caught.
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


# --------------------------------------------------------------------------- #
# Deny contract over the wire (JSON on stdout, exit 0)
# --------------------------------------------------------------------------- #


def assert_asked(parsed, contains=None):
    assert parsed is not None, "expected an ask JSON object, got no stdout"
    hs = parsed["hookSpecificOutput"]
    assert hs["permissionDecision"] == "ask"
    assert hs["permissionDecisionReason"] and "\n" not in hs["permissionDecisionReason"]
    if contains:
        assert contains in hs["permissionDecisionReason"]


def test_wire_ask_skip_perms(tmp_path):
    rc, parsed = run_hook(bash("claude --dangerously-skip-permissions"), tmp_path)
    assert rc == 0
    assert_asked(parsed, contains="permission")


def test_wire_allow_normal_command(tmp_path):
    rc, parsed = run_hook(bash("git status"), tmp_path)
    assert rc == 0
    assert parsed is None  # no stdout == allow


# --------------------------------------------------------------------------- #
# Escape hatches
# --------------------------------------------------------------------------- #


def test_bypass_env_disables_all(tmp_path):
    rc, parsed = run_hook(
        bash("claude --dangerously-skip-permissions"),
        tmp_path,
        env={"CLAUDE_HOOK_BYPASS": "1"},
    )
    assert parsed is None


def test_stop_list_switch_turns_off_every_builtin_ask(tmp_path):
    rc, parsed = run_hook(
        bash("claude --dangerously-skip-permissions"),
        tmp_path,
        env={"GOODFELLOW_STOP_LIST": "0"},
    )
    assert parsed is None


@pytest.mark.parametrize(
    "cmd",
    [
        "git add -A",
        "git push --force origin main",
        "claude --dangerously-skip-permissions",
        "gh pr create --fill",
        "npm publish",
    ],
)
def test_builtins_never_deny(tmp_path, cmd):
    """A built-in never blocks: at most it asks, and the user's yes goes through."""
    d = decide(bash(cmd), str(tmp_path))
    assert d is None or d[0] == "ask", d


def test_only_the_permission_skip_builtin_remains():
    assert BUILTIN_IDS == ("dangerous-skip-permissions",)


def test_retired_ids_and_keys_in_old_configs_are_accepted(tmp_path):
    write_guards(
        tmp_path,
        {
            "disable_builtins": [
                "git-add-all",
                "force-push-protected",
                "stop-public-repo",
                "stop-foreign-remote",
                "stop-migration",
            ],
            "stop_list": {"owners": ["me"], "migration_commands": ["make migrate"]},
        },
    )
    assert load_config(str(tmp_path))["disable_builtins"]


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


def test_disable_builtin_from_config(tmp_path):
    write_guards(tmp_path, {"disable_builtins": ["dangerous-skip-permissions"]})
    _, parsed = run_hook(bash("claude --dangerously-skip-permissions"), tmp_path)
    assert parsed is None  # disabled


def test_regex_redos_bounded_and_denies(tmp_path):
    # A pathological pattern must not wedge the hook: the bounded worker is killed
    # at the budget and the operation is conservatively DENIED (not silently allowed).
    write_guards(
        tmp_path,
        {
            "block": [
                {"id": "redos", "match": "regex", "pattern": "(a+)+$", "reason": "x"}
            ]
        },
    )
    payload = bash("a" * 60 + "!")
    import time

    start = time.time()
    _, parsed = run_hook(payload, tmp_path)
    elapsed = time.time() - start
    assert elapsed < 15, f"hook took {elapsed}s — ReDoS not bounded"
    assert_denied(parsed, contains="could not be evaluated")


def test_regex_full_payload_evaluated_no_truncation(tmp_path):
    # a match after a long safe prefix must still be caught (no length cap
    # that a dangerous suffix could hide behind).
    write_guards(
        tmp_path,
        {
            "block": [
                {
                    "id": "no-block",
                    "match": "regex",
                    "pattern": "BLOCK",
                    "reason": "nope",
                }
            ]
        },
    )
    _, parsed = run_hook(bash("x" * 200_000 + " BLOCK"), tmp_path)
    assert_denied(parsed, contains="no-block")


def test_many_regex_rules_share_one_budget(tmp_path):
    # N pathological rules must not cost N * per-rule-timeout. One aggregate
    # budget covers them all, so the hook still returns quickly.
    write_guards(
        tmp_path,
        {
            "block": [
                {
                    "id": f"redos{i}",
                    "match": "regex",
                    "pattern": "(a+)+$",
                    "reason": "x",
                }
                for i in range(20)
            ]
        },
    )
    payload = bash("a" * 60 + "!")
    import time

    start = time.time()
    _, parsed = run_hook(payload, tmp_path)
    elapsed = time.time() - start
    assert elapsed < 15, f"20 rules took {elapsed}s — budget is per-rule, not aggregate"
    assert_denied(parsed, contains="could not be evaluated")


def test_substring_only_config_fast_path(tmp_path):
    # Pure-substring configs take the inline fast path (correctness check).
    write_guards(
        tmp_path,
        {
            "block": [
                {"id": "s1", "pattern": "AAA", "reason": "x"},
                {"id": "s2", "pattern": "BBB", "reason": "y"},
            ]
        },
    )
    _, hit = run_hook(bash("echo BBB"), tmp_path)
    assert_denied(hit, contains="s2")
    _, miss = run_hook(bash("echo ok"), tmp_path)
    assert miss is None


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
    # ...but the built-in still asks.
    _, asked = run_hook(bash("claude --dangerously-skip-permissions"), tmp_path)
    assert_asked(asked)


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
        },  # non-str flags
        {
            "block": [{"id": "x", "pattern": "y", "reason": "z", "flags": "q"}]
        },  # bad flag char
        {
            "block": [{"id": "x", "pattern": "y", "reason": "z", "bypass_env": 1}]
        },  # non-str env
        {
            "block": [{"id": "x", "pattern": "y", "reason": "z", "bypass_env": "1BAD"}]
        },  # bad env name
        {
            "block": [  # duplicate ids
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
    # same id, changed pattern -> digest changes, so drift is detected.
    write_guards(tmp_path, {"block": [{"id": "r", "pattern": "A", "reason": "x"}]})
    before = active_guard_set(str(tmp_path))["user_rules"][0]["digest"]
    write_guards(tmp_path, {"block": [{"id": "r", "pattern": "B", "reason": "x"}]})
    after = active_guard_set(str(tmp_path))["user_rules"][0]["digest"]
    assert before != after


def test_assert_guard_set_detects_drift(tmp_path):
    # --assert-guard-set exits non-zero when the set changes, not just warns.
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
# pin the host wiring (guard is dead if hooks.json stops calling it)
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


# --------------------------------------------------------------------------- #
# the snap-compact drift gate must not mask its own non-zero exit
# --------------------------------------------------------------------------- #


def test_snap_compact_drift_gate_not_masked():
    skill = os.path.join(os.path.dirname(HERE), "skills", "snap-compact", "SKILL.md")
    with open(skill, "r", encoding="utf-8") as fh:
        text = fh.read()
    # The assertion must be gated by `if !`, and must NOT swallow the exit via
    # `|| echo` (which would report success precisely when governance drifted).
    assert "--assert-guard-set" in text
    assert "if ! python3" in text
    assert (
        "--assert-guard-set .goodfellow/guard-set.pre-compact.json \\\n     || echo"
        not in text
    )
    # No `|| echo` on the same logical line as the assertion.
    for line in text.splitlines():
        if "assert-guard-set" in line:
            assert "|| echo" not in line


# --------------------------------------------------------------------------- #
# Value-taking push options must not shift remote/refspec parsing
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# --validate is a genuine ReDoS diagnostic
# --------------------------------------------------------------------------- #


def test_validate_warns_on_redos_prone_pattern(tmp_path):
    write_guards(
        tmp_path,
        {
            "block": [
                {"id": "danger", "match": "regex", "pattern": "(a+)+$", "reason": "x"},
                {"id": "safe", "match": "regex", "pattern": "^abc$", "reason": "y"},
            ]
        },
    )
    proc = subprocess.run(
        [sys.executable, ENGINE, "--project-dir", str(tmp_path), "--validate"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0  # risk is a warning, not an error
    assert "danger" in proc.stderr
    assert "safe" not in proc.stderr


# --------------------------------------------------------------------------- #
# Fail-open gaps found by mutation testing (each test names the break it kills)
# --------------------------------------------------------------------------- #


def _assert_guard_set(tmp_path, baseline_path):
    return subprocess.run(
        [
            sys.executable,
            ENGINE,
            "--project-dir",
            str(tmp_path),
            "--assert-guard-set",
            str(baseline_path),
        ],
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("baseline_text", [None, "{ not json"])
def test_assert_guard_set_unreadable_baseline_is_drift(tmp_path, baseline_text):
    # Break killed: the "cannot read baseline" branch returning None (exit 0).
    baseline = tmp_path / "baseline.json"
    if baseline_text is not None:
        baseline.write_text(baseline_text)
    proc = _assert_guard_set(tmp_path, baseline)
    assert proc.returncode == 1
    assert "cannot read baseline" in proc.stderr


def test_assert_guard_set_config_error_is_drift_even_when_baseline_matches(tmp_path):
    # A baseline snapshotted from an already-broken config equals the current
    # set, so only the config-error branch stands between a disarmed rule set and
    # "OK". Break killed: that branch returning None (exit 0).
    gf = tmp_path / ".goodfellow"
    gf.mkdir()
    (gf / "guards.json").write_text("{ broken ")
    snap = subprocess.run(
        [sys.executable, ENGINE, "--project-dir", str(tmp_path), "--selfcheck"],
        capture_output=True,
        text=True,
    )
    baseline = tmp_path / "baseline.json"
    baseline.write_text(snap.stdout)
    proc = _assert_guard_set(tmp_path, baseline)
    assert proc.returncode == 1
    assert "config error" in proc.stderr
    assert "OK" not in proc.stdout


@pytest.mark.parametrize(
    "bad",
    [
        {"disable_builtins": {}},  # not a list, and iterates as empty
        {"block": {}},  # not a list, and iterates as empty
        {"block": ["no-prod"]},  # rule is not an object
        {"block": [{"id": "x", "pattern": "y", "reason": "z", "tools": "Bash"}]},
        {"block": [{"id": "x", "pattern": "y", "reason": "z", "tools": [""]}]},
        {"block": [{"id": 5, "pattern": "y", "reason": "z"}]},  # truthy non-string
    ],
)
def test_validate_config_rejects_shapes_that_iterate_as_valid(bad):
    # Break killed: each `raise GuardConfigError` becoming `pass`. These shapes
    # would otherwise load as "no rules" (or a substring tool match) silently.
    with pytest.raises(GuardConfigError):
        validate_config(bad)


def test_load_config_rejects_non_object_json(tmp_path):
    # Break killed: the "must be a JSON object" raise becoming `pass`.
    write_guards(tmp_path, [{"id": "x", "pattern": "y", "reason": "z"}])
    with pytest.raises(GuardConfigError):
        load_config(str(tmp_path))


def test_validate_cli_fails_loud_on_rules_that_are_not_a_list(tmp_path):
    write_guards(tmp_path, {"block": {}})
    proc = subprocess.run(
        [sys.executable, ENGINE, "--project-dir", str(tmp_path), "--validate"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "INVALID" in proc.stderr


@pytest.mark.parametrize(
    "flags,pattern,hit,miss",
    [
        ("i", r"drop\s+table", "psql -c 'DROP TABLE users'", "psql -c 'select 1'"),
        ("m", r"^rm -rf", "echo ok\nrm -rf build", "echo rm -rf build"),
        ("s", r"begin.*commit", "begin\nwork\ncommit", "begin work"),
    ],
)
def test_regex_flags_are_honoured_and_only_when_set(
    tmp_path, flags, pattern, hit, miss
):
    # Break killed: `if "<flag>" in flags` negated or dropped in `_regex_flags`.
    rule = {"id": "r", "match": "regex", "pattern": pattern, "reason": "x"}
    write_guards(tmp_path, {"block": [dict(rule, flags=flags)]})
    assert decision_for_input(bash(hit), str(tmp_path)) is not None
    assert decision_for_input(bash(miss), str(tmp_path)) is None
    # Without the flag the same text must not match: the flag is doing the work.
    write_guards(tmp_path, {"block": [rule]})
    assert decision_for_input(bash(hit), str(tmp_path)) is None


def test_user_rule_matches_edit_new_string(tmp_path):
    # Break killed: `extract_text` returning None for Edit.
    rule = {
        "id": "no-secret",
        "pattern": "SECRET_TOKEN",
        "reason": "x",
        "tools": ["Edit"],
    }
    write_guards(tmp_path, {"block": [rule]})
    edit = {
        "tool_name": "Edit",
        "tool_input": {"old_string": "a", "new_string": "SECRET_TOKEN=1"},
    }
    assert_denied(run_hook(edit, tmp_path)[1], contains="no-secret")
    clean = {
        "tool_name": "Edit",
        "tool_input": {"old_string": "SECRET_TOKEN", "new_string": "b"},
    }
    assert run_hook(clean, tmp_path)[1] is None


def test_selfcheck_reports_hook_bypass(tmp_path, monkeypatch):
    # Break killed: `all_disabled` comparison swapped, which would snapshot every
    # baseline as "all guards off".
    monkeypatch.delenv("CLAUDE_HOOK_BYPASS", raising=False)
    assert active_guard_set(str(tmp_path))["all_disabled"] is False
    monkeypatch.setenv("CLAUDE_HOOK_BYPASS", "1")
    assert active_guard_set(str(tmp_path))["all_disabled"] is True
