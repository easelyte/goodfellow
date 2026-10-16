"""Custom runners exercise the real CLI and throwaway head/base directories."""

import json
import pathlib
import shlex
import subprocess
import sys

import pytest

import red_check
from test_red_check import SCRIPT, _repo, _commit, _run, _git, BASE_TESTS, FIXED_CALC


def configured_repo(tmp_path, config):
    repo, _ = _repo(tmp_path)
    base = _commit(
        repo, {".goodfellow/config.json": json.dumps(config)}, "runner config"
    )
    _commit(
        repo,
        {
            "calc.py": FIXED_CALC,
            "test_calc.py": BASE_TESTS
            + "\n\ndef test_negative():\n    assert clamp(-1) == 0\n",
        },
        "fix",
    )
    return repo, base


@pytest.mark.parametrize(
    "config",
    [
        [],
        {"red_check": []},
        {"red_check": None},
        {"red_check": {"command": 1}},
        {"red_check": {"reset": False}},
        {"red_check": {"unknown": "true"}},
        {"red_check": {"command": ""}},
        {"red_check": {"reset": ""}},
        {"red_check": {"reset": "   "}},
    ],
)
def test_invalid_runner_config_fails_loudly(tmp_path, config):
    repo, base = configured_repo(tmp_path, config)
    result = _run(repo, "--base", base)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "config.json" in result.stderr


def test_invalid_json_fails_even_with_cli_command(tmp_path):
    repo, base = configured_repo(tmp_path, {})
    base = _commit(repo, {".goodfellow/config.json": "{broken"}, "bad config")
    result = _run(repo, "--base", base, "--command", "true")
    assert result.returncode == 2
    assert "config.json" in result.stderr


def test_workdir_junit_reset_order_and_sandbox_reporting(tmp_path, monkeypatch):
    log = tmp_path / "order.jsonl"
    scratch = tmp_path / "runner scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    # A fake container boundary: require JUnit to be writable through the sole mount.
    runner = (
        "import json,pathlib,sys; w=pathlib.Path(sys.argv[1]); "
        "j=pathlib.Path(sys.argv[2]); assert not j.is_absolute(); j=j.resolve(); assert w==pathlib.Path.cwd(); "
        'assert j.parent==w; assert (w/"ready").read_text()=="yes"; '
        '(w/"ready").unlink(); '
        'open(sys.argv[3],"a").write(json.dumps(["run",str(w),str(j)])+"\\n"); '
        "j.write_text('<testsuite><testcase name=\"new\"/></testsuite>')"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(runner)} {{workdir}} {{junit}} {shlex.quote(str(log))}"
    reset = f"printf yes > {{workdir}}/ready; printf '[\"reset\"]\\n' >> {shlex.quote(str(log))}"
    repo, base = configured_repo(
        tmp_path, {"red_check": {"command": command, "reset": reset}}
    )
    monkeypatch.setenv("GOODFELLOW_SANDBOX", "invalid-even-if-installed")
    result = _run(repo, "--base", base)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["sandbox"] == "custom-runner"
    assert "custom runner" in report["note"] and "skipped" in report["note"]
    assert result.stderr.count(report["note"]) == 1
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert [e[0] for e in events] == ["reset", "run"] * 3
    assert events[1][1].endswith("/head") and events[3][1].endswith("/base")
    assert events[3][1] == events[5][1]
    text = subprocess.run(
        [sys.executable, str(SCRIPT), "--workdir", str(repo), "--base", base],
        capture_output=True,
        text=True,
    )
    assert report["note"] in text.stdout


def test_cli_command_and_reset_override_config(tmp_path):
    repo, base = configured_repo(
        tmp_path, {"red_check": {"command": "false", "reset": "exit 9"}}
    )
    cmd = f"{shlex.quote(sys.executable)} -m pytest -q --rootdir=. {{tests}} --junitxml={{junit}}"
    result = _run(repo, "--base", base, "--command", cmd, "--reset", "true")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["results"][0]["verdict"] == "OK"


def test_reset_failure_stops_before_test_command(tmp_path):
    marker = tmp_path / "ran"
    repo, base = configured_repo(
        tmp_path,
        {
            "red_check": {
                "command": f"touch {shlex.quote(str(marker))}",
                "reset": "exit 7",
            }
        },
    )
    result = _run(repo, "--base", base)
    assert result.returncode == 2
    assert "reset" in result.stderr and "7" in result.stderr
    assert not marker.exists()


@pytest.mark.parametrize("failing_run", [2, 3])
def test_failure_on_either_base_reset_stops_before_that_run(tmp_path, failing_run):
    log = tmp_path / "events"
    state = tmp_path / "count"
    reset_code = (
        "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
        "n=int(p.read_text())+1 if p.exists() else 1; p.write_text(str(n)); "
        "sys.exit(8 if n==int(sys.argv[2]) else 0)"
    )
    reset = f"{shlex.quote(sys.executable)} -c {shlex.quote(reset_code)} {shlex.quote(str(state))} {failing_run}"
    command = f"echo run >> {shlex.quote(str(log))}; printf '<testsuite><testcase name=\"new\"/></testsuite>' > {{junit}}"
    repo, base = configured_repo(
        tmp_path, {"red_check": {"command": command, "reset": reset}}
    )
    result = _run(repo, "--base", base)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "reset failed in base" in result.stderr
    assert len(log.read_text().splitlines()) == failing_run - 1
    assert len(_git(repo, "worktree", "list").splitlines()) == 1


def test_junit_is_removed_when_custom_runner_times_out(tmp_path, monkeypatch):
    def timed_out(cmd, cwd, env, timeout, sweep):
        # Write exactly the report filename substituted into the command.
        pathlib.Path(cwd / shlex.split(cmd)[-1]).write_text("<testsuite/>")
        return None, "", ""

    monkeypatch.setattr(red_check.proc_group, "run", timed_out)
    assert red_check.run_tests(tmp_path, [], "fake {workdir} {junit}", 1) is None
    assert list(tmp_path.glob("red-check-*.xml")) == []


def test_config_preserves_other_top_level_sections(tmp_path):
    repo, base = configured_repo(
        tmp_path,
        {
            "memory": {"mode": "flat"},
            "red_check": {"command": "pytest", "reset": "true"},
        },
    )
    assert red_check.runner_config(repo, base) == {"command": "pytest", "reset": "true"}


def test_runner_config_from_subdirectory_reads_repo_root_base_config(tmp_path):
    settings = {"command": "pytest", "reset": "true"}
    repo, base = configured_repo(tmp_path, {"red_check": settings})
    subdir = repo / "nested"
    subdir.mkdir()
    _commit(repo, {".goodfellow/config.json": "{broken"}, "head config")
    assert red_check.runner_config(subdir, base) == settings


@pytest.mark.parametrize("source", ["config", "cli", "both"])
def test_test_cmd_rejects_custom_runner_command(tmp_path, source):
    config = {"red_check": {"command": "true"}} if source != "cli" else {}
    repo, base = configured_repo(tmp_path, config)
    args = ["--command", "true"] if source != "config" else []
    result = _run(repo, "--base", base, "--test-cmd", "true", *args)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "--test-cmd cannot be combined with a custom runner command" in result.stderr
    assert "sandbox skipped" not in result.stderr


@pytest.mark.parametrize("flag", ["--command", "--reset"])
@pytest.mark.parametrize("value", ["", "   "])
def test_empty_cli_runner_values_rejected(tmp_path, flag, value):
    repo, base = _repo(tmp_path)
    result = _run(repo, "--base", base, flag, value)
    assert result.returncode == 2
    assert f"{flag} must be a nonempty string" in result.stderr


@pytest.mark.parametrize("args", [("--reset", "true"), ("--test-cmd", "true")])
def test_reset_alone_and_test_cmd_keep_sandbox(tmp_path, monkeypatch, args):
    repo, base = _repo(tmp_path)
    monkeypatch.setenv("GOODFELLOW_SANDBOX", "invalid")
    result = _run(repo, "--base", base, *args)
    assert result.returncode == 2
    assert "GOODFELLOW_SANDBOX" in result.stderr
    assert "custom runner" not in result.stderr


def test_reset_timeout_stops_before_runner(tmp_path, monkeypatch):
    calls = []

    def timeout(cmd, *args, **kwargs):
        calls.append(cmd)
        return None, "", ""

    monkeypatch.setattr(red_check.proc_group, "run", timeout)
    with pytest.raises(red_check.RedCheckError, match="reset timed out") as exc:
        red_check.run_tests(tmp_path, [], "must not run", 1, reset="sleep 10")
    assert "exit None" not in str(exc.value)
    assert calls == ["sleep 10"]


def test_head_config_cannot_override_trusted_base(tmp_path, monkeypatch):
    repo, base = configured_repo(tmp_path, {"red_check": {"command": "true"}})
    _commit(repo, {".goodfellow/config.json": "{broken"}, "hostile config")
    monkeypatch.setenv("GOODFELLOW_SANDBOX", "invalid")
    result = _run(repo, "--base", base)
    assert result.returncode == 2
    assert "no JUnit report" in result.stderr
    assert "sandbox skipped" in result.stderr


def test_head_only_config_is_ignored(tmp_path, monkeypatch):
    repo, base = _repo(tmp_path)
    _commit(
        repo,
        {".goodfellow/config.json": json.dumps({"red_check": {"command": "true"}})},
        "hostile config",
    )
    monkeypatch.setenv("GOODFELLOW_SANDBOX", "invalid")
    result = _run(repo, "--base", base)
    assert result.returncode == 2
    assert "GOODFELLOW_SANDBOX" in result.stderr
    assert "custom runner" not in result.stderr


def test_substitution_does_not_expand_placeholders_in_values(tmp_path, monkeypatch):
    commands = []

    def run(cmd, *args, **kwargs):
        commands.append(shlex.split(cmd))
        return 0, "", ""

    monkeypatch.setattr(red_check.proc_group, "run", run)
    red_check.run_tests(
        tmp_path, ["test_{junit}_{workdir}.py"], "runner {tests} {junit} {workdir}", 1
    )
    assert commands[0][1] == "test_{junit}_{workdir}.py"
    assert commands[0][-1] == str(tmp_path)


@pytest.mark.parametrize("failure", ["worktree", "rmtree"])
def test_cleanup_failure_warns_with_leftover_path(
    tmp_path, monkeypatch, capsys, failure
):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(red_check.tempfile, "mkdtemp", lambda **kwargs: str(scratch))

    def git(workdir, args, check=True):
        if args[:2] == ["worktree", "add"]:
            (scratch / "base").mkdir()
        if args[:2] == ["worktree", "remove"] and failure == "worktree":
            raise red_check.RedCheckError("permission denied")
        return "base-sha"

    monkeypatch.setattr(red_check, "_git", git)
    monkeypatch.setattr(red_check, "_copy_worktree", lambda *args: None)
    monkeypatch.setattr(
        red_check, "run_tests", lambda *args: {"new": ("pass", None, "")}
    )

    def rmtree(path):
        if failure == "rmtree":
            raise PermissionError("root-owned file")

    monkeypatch.setattr(red_check.shutil, "rmtree", rmtree)
    monkeypatch.setattr(red_check.proc_group, "sweep_cwd", lambda *args: None)
    red_check.check(tmp_path, "base", ["test_new.py"], [], "fake", 1, (), (), True)
    warning = capsys.readouterr().err
    leftover = scratch / "base" if failure == "worktree" else scratch
    assert "WARNING" in warning
    assert f"leftover path {leftover}:" in warning
