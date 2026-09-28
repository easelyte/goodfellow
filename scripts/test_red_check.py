"""Tests for red_check.py: new tests must go red for the RIGHT reason on the base.

Each planted case builds a real throwaway git repo (base commit -> head commit),
runs red_check.py as a subprocess with the real pytest runner, and asserts the
verdict for the planted test. The planted cases are the whole point: a check
that has never been shown to reject a bad test is a check that can't fail.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import red_check

SCRIPT = Path(__file__).parent / "red_check.py"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(repo: Path, files: dict, msg: str) -> str:
    for name, body in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
        _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


BASE_CALC = "def clamp(x):\n    return x\n"
FIXED_CALC = "def clamp(x):\n    return max(0, x)\n"
BASE_TESTS = (
    "from calc import clamp\n\n\ndef test_positive():\n    assert clamp(3) == 3\n"
)


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "tester@example.com")
    _git(repo, "config", "user.name", "tester")
    base = _commit(
        repo,
        {
            "calc.py": BASE_CALC,
            "test_calc.py": BASE_TESTS,
            ".gitignore": "__pycache__/\n",
        },
        "base",
    )
    return repo, base


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--workdir", str(repo), "--json", *args],
        capture_output=True,
        text=True,
    )


def _verdicts(proc: subprocess.CompletedProcess) -> dict:
    data = json.loads(proc.stdout)
    return {r["test"]: r["verdict"] for r in data["results"]}


# --- planted cases ----------------------------------------------------------


def test_assertion_red_then_green_is_ok(tmp_path):
    repo, base = _repo(tmp_path)
    _commit(
        repo,
        {
            "calc.py": FIXED_CALC,
            "test_calc.py": BASE_TESTS
            + "\n\ndef test_negative_clamps_to_zero():\n    assert clamp(-1) == 0\n",
        },
        "fix",
    )
    proc = _run(repo, "--base", base)
    assert _verdicts(proc) == {"test_calc::test_negative_clamps_to_zero": "OK"}, (
        proc.stdout
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_missing_symbol_on_base_is_new_symbol_not_ok(tmp_path):
    # The new test fails on the base only because the function does not exist
    # yet (AttributeError). A replay cannot judge it: that red says nothing about
    # the behaviour, so it is reported apart from OK, and fails under --strict.
    repo, base = _repo(tmp_path)
    _commit(
        repo,
        {
            "calc.py": FIXED_CALC + "\n\ndef double(x):\n    return 2 * x\n",
            "test_calc.py": BASE_TESTS
            + "\n\nimport calc\n\n\ndef test_double():\n    assert calc.double(2) == 4\n",
        },
        "add double",
    )
    proc = _run(repo, "--base", base)
    assert _verdicts(proc) == {"test_calc::test_double": "NEW_SYMBOL"}, proc.stdout
    assert proc.returncode == 0
    proc = _run(repo, "--base", base, "--strict")
    assert proc.returncode == 1


def test_import_error_at_collection_is_new_symbol(tmp_path):
    repo, base = _repo(tmp_path)
    _commit(
        repo,
        {
            "calc.py": FIXED_CALC + "\n\ndef triple(x):\n    return 3 * x\n",
            "test_triple.py": "from calc import triple\n\n\ndef test_triple():\n"
            "    assert triple(2) == 6\n",
        },
        "add triple",
    )
    proc = _run(repo, "--base", base)
    assert _verdicts(proc) == {"test_triple::test_triple": "NEW_SYMBOL"}, proc.stdout
    assert proc.returncode == 0
    (result,) = json.loads(proc.stdout)["results"]
    assert "triple" in result["base_message"], result


def test_crash_in_existing_code_is_wrong_reason(tmp_path):
    # clamp exists on the base but not with this signature: the red is a
    # TypeError from the call, not an assertion about the behaviour.
    repo, base = _repo(tmp_path)
    _commit(
        repo,
        {
            "calc.py": "def clamp(x, lo=0):\n    return max(lo, x)\n",
            "test_calc.py": BASE_TESTS
            + "\n\ndef test_floor():\n    assert clamp(-1, lo=0) == 0\n",
        },
        "add floor",
    )
    proc = _run(repo, "--base", base)
    assert _verdicts(proc) == {"test_calc::test_floor": "WRONG_REASON"}, proc.stdout
    assert proc.returncode == 1


def test_new_test_in_a_new_directory_is_replayed(tmp_path):
    repo, base = _repo(tmp_path)
    _commit(
        repo,
        {
            "calc.py": FIXED_CALC,
            "tests/test_more.py": "import sys, pathlib\n"
            "sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))\n"
            "from calc import clamp\n\n\ndef test_neg():\n    assert clamp(-2) == 0\n",
        },
        "tests dir",
    )
    proc = _run(repo, "--base", base)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert list(_verdicts(proc).values()) == ["OK"], proc.stdout


def test_test_that_passes_on_base_is_not_red(tmp_path):
    repo, base = _repo(tmp_path)
    _commit(
        repo,
        {
            "calc.py": FIXED_CALC,
            "test_calc.py": BASE_TESTS
            + "\n\ndef test_zero():\n    assert clamp(0) == 0\n",
        },
        "tautological test",
    )
    proc = _run(repo, "--base", base)
    assert _verdicts(proc) == {"test_calc::test_zero": "NOT_RED"}, proc.stdout
    assert proc.returncode == 1


def test_test_failing_on_head_is_not_green(tmp_path):
    repo, base = _repo(tmp_path)
    _commit(
        repo,
        {
            "test_calc.py": BASE_TESTS
            + "\n\ndef test_negative():\n    assert clamp(-5) == 0\n",
        },
        "test without the fix",
    )
    proc = _run(repo, "--base", base)
    assert _verdicts(proc) == {"test_calc::test_negative": "NOT_GREEN"}, proc.stdout
    assert proc.returncode == 1


def test_did_not_raise_counts_as_right_reason(tmp_path):
    repo, base = _repo(tmp_path)
    _commit(
        repo,
        {
            "calc.py": "def clamp(x):\n    if x is None:\n        raise ValueError('x')\n"
            "    return max(0, x)\n",
            "test_calc.py": BASE_TESTS
            + "\n\nimport pytest\n\n\ndef test_none_rejected():\n"
            "    with pytest.raises(ValueError):\n        clamp(None)\n",
        },
        "reject None",
    )
    proc = _run(repo, "--base", base)
    assert _verdicts(proc) == {"test_calc::test_none_rejected": "OK"}, proc.stdout
    assert proc.returncode == 0


# --- no-op and fail-closed paths -------------------------------------------


def test_no_new_tests_passes_unless_required(tmp_path):
    repo, base = _repo(tmp_path)
    _commit(repo, {"calc.py": FIXED_CALC}, "code only")
    proc = _run(repo, "--base", base)
    assert json.loads(proc.stdout)["results"] == []
    assert proc.returncode == 0
    proc = _run(repo, "--base", base, "--require-tests")
    assert proc.returncode == 1


def test_unknown_base_fails_closed(tmp_path):
    repo, _ = _repo(tmp_path)
    proc = _run(repo, "--base", "no-such-ref")
    assert proc.returncode == 2
    assert "no-such-ref" in proc.stderr


def test_head_runner_crash_fails_closed(tmp_path):
    repo, base = _repo(tmp_path)
    _commit(
        repo,
        {
            "test_calc.py": BASE_TESTS
            + "\n\ndef test_new():\n    assert clamp(-1) == 0\n"
        },
        "t",
    )
    proc = _run(repo, "--base", base, "--test-cmd", "false {tests} {junit}")
    assert proc.returncode == 2, proc.stdout + proc.stderr


def test_leaves_no_worktree_and_does_not_touch_the_checkout(tmp_path):
    repo, base = _repo(tmp_path)
    _commit(
        repo,
        {
            "calc.py": FIXED_CALC,
            "test_calc.py": BASE_TESTS
            + "\n\ndef test_n():\n    assert clamp(-1) == 0\n",
        },
        "fix",
    )
    before = (repo / "calc.py").read_text()
    _run(repo, "--base", base)
    assert (repo / "calc.py").read_text() == before
    assert len(_git(repo, "worktree", "list").splitlines()) == 1
    assert _git(repo, "status", "--porcelain") == ""


# --- classification unit ----------------------------------------------------


def test_missing_symbol_detection():
    m = red_check.is_missing_symbol
    assert m("AttributeError: module 'calc' has no attribute 'double'")
    assert m("ImportError: cannot import name 'triple' from 'calc'")
    assert m("ModuleNotFoundError: No module named 'newmod'")
    assert m("NameError: name 'helper' is not defined")
    assert not m("TypeError: clamp() got an unexpected keyword argument 'lo'")
    assert not m("assert 1 == 2")


def test_classify_failure_messages():
    c = red_check.classify_outcome
    assert c("failure", None, "assert 1 == 2") == "assertion"
    assert c("failure", None, "expect(received).toBe(expected)") == "assertion"
    assert c("failure", "ValueError", "") == "exception"
    assert c("failure", None, "Cannot read properties of undefined") == "unknown"
    assert c("failure", None, "Error: boom") == "unknown"
    assert c("failure", None, "AssertionError: nope") == "assertion"
    assert (
        c("failure", None, "Failed: DID NOT RAISE <class 'ValueError'>") == "assertion"
    )
    assert c("failure", "AssertionError", "expected 1 to be 2") == "assertion"
    assert c("failure", None, "NameError: name 'foo' is not defined") == "exception"
    assert c("failure", None, "ModuleNotFoundError: No module named 'x'") == "exception"
    assert c("failure", "TypeError", "boom") == "exception"
    assert c("error", None, "collection failure") == "exception"
    assert c("pass", None, "") == "pass"
    assert c("skipped", None, "") == "skipped"


def test_extra_assertion_pattern_is_honoured():
    c = red_check.classify_outcome
    assert (
        c("failure", None, "Mismatch: wanted 2", patterns=(r"^Mismatch:",))
        == "assertion"
    )


def test_extra_assertion_type_is_honoured():
    c = red_check.classify_outcome
    assert c("failure", None, "ContractError: x") == "exception"
    assert c("failure", None, "ContractError: x", ("ContractError",)) == "assertion"


def test_tests_that_write_files_do_not_touch_the_checkout(tmp_path):
    repo, base = _repo(tmp_path)
    _commit(
        repo,
        {
            "calc.py": FIXED_CALC,
            "test_calc.py": BASE_TESTS + "\n\ndef test_writes():\n"
            "    open('touched.txt', 'w').write('x')\n"
            "    assert clamp(-1) == 0\n",
        },
        "writer",
    )
    proc = _run(repo, "--base", base)
    assert _verdicts(proc) == {"test_calc::test_writes": "OK"}, proc.stdout
    assert not (repo / "touched.txt").exists()


def test_duplicate_junit_ids_fail_closed(tmp_path):
    xml = tmp_path / "r.xml"
    xml.write_text(
        '<testsuites><testsuite name="a"><testcase classname="c" name="t"/>'
        '</testsuite><testsuite name="b"><testcase classname="c" name="t">'
        '<failure message="assert 0"/></testcase></testsuite></testsuites>'
    )
    try:
        red_check.parse_junit(xml)
    except red_check.RedCheckError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate test ids were silently merged")


def test_existing_tests_in_changed_files_are_listed_as_not_replayed(tmp_path):
    repo, base = _repo(tmp_path)
    _commit(
        repo,
        {
            "calc.py": FIXED_CALC,
            "test_calc.py": BASE_TESTS.replace("clamp(3) == 3", "clamp(4) == 4")
            + "\n\ndef test_n():\n    assert clamp(-1) == 0\n",
        },
        "edit existing expectation",
    )
    proc = _run(repo, "--base", base)
    assert json.loads(proc.stdout)["not_replayed"] == ["test_calc::test_positive"]
    assert "--all-tests" in proc.stderr
    proc = _run(repo, "--base", base, "--all-tests")
    assert _verdicts(proc)["test_calc::test_positive"] == "NOT_RED"


def test_skipped_new_test_is_not_a_pass(tmp_path):
    repo, base = _repo(tmp_path)
    _commit(
        repo,
        {
            "calc.py": FIXED_CALC,
            "test_calc.py": BASE_TESTS
            + "\n\nimport pytest\n\n\n@pytest.mark.skip\ndef test_later():\n"
            "    assert clamp(-1) == 0\n",
        },
        "skipped test",
    )
    proc = _run(repo, "--base", base)
    assert _verdicts(proc) == {"test_calc::test_later": "SKIPPED"}, proc.stdout
    assert proc.returncode == 1


def test_unrecognised_base_failure_is_unclear_and_fails(tmp_path):
    repo, base = _repo(tmp_path)
    _commit(
        repo,
        {
            "calc.py": FIXED_CALC,
            "test_calc.py": BASE_TESTS + "\n\nimport pytest\n\n\ndef test_odd():\n"
            "    if clamp(-1) != 0:\n        raise RuntimeError('odd')\n",
        },
        "odd failure",
    )
    # RuntimeError is named, so it is an exception; a bare message is unknown.
    proc = _run(repo, "--base", base)
    assert _verdicts(proc) == {"test_calc::test_odd": "WRONG_REASON"}, proc.stdout
    assert red_check.verdict_for("pass", "unknown", "") == "UNCLEAR"
    assert "UNCLEAR" in red_check.BAD


def test_report_with_no_testcases_fails_closed(tmp_path):
    repo, base = _repo(tmp_path)
    _commit(
        repo,
        {"test_calc.py": BASE_TESTS + "\n\ndef test_x():\n    assert clamp(-1) == 0\n"},
        "t",
    )
    cmd = "printf '<testsuites/>' > {junit} # {tests}"
    proc = _run(repo, "--base", base, "--test-cmd", cmd)
    assert proc.returncode == 2, proc.stdout + proc.stderr


def test_head_runner_failing_without_a_reported_failure_fails_closed(tmp_path):
    # A report that shows only passes while the runner exits nonzero is partial:
    # some test was dropped, so no verdict is trustworthy.
    repo, base = _repo(tmp_path)
    _commit(
        repo,
        {
            "calc.py": FIXED_CALC,
            "test_calc.py": BASE_TESTS
            + "\n\ndef test_x():\n    assert clamp(-1) == 0\n",
        },
        "t",
    )
    cmd = (
        f"{sys.executable} -m pytest -q -p no:cacheprovider {{tests}} "
        "--junitxml={junit}; exit 3"
    )
    proc = _run(repo, "--base", base, "--test-cmd", cmd)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "exit" in proc.stderr


def _pidns_prefix():
    unshare = shutil.which("unshare")
    if not unshare:
        return None
    variants = [[]] if os.geteuid() == 0 else []
    variants.append(["--user", "--map-root-user"])
    for extra in variants:
        cmd = [unshare, *extra, "--pid", "--fork", "--mount-proc"]
        try:
            ok = subprocess.run(
                [*cmd, "sh", "-c", "test $$ -eq 1"], capture_output=True, timeout=10
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if ok.returncode == 0:
            return cmd
    return None


PIDNS = _pidns_prefix()

# Runs as pid 1 of a private PID namespace: starts the tool, waits for it, then
# lists every other live process in the namespace. Anything listed is a leak.
IN_NS = """
import json, os, pathlib, subprocess, sys, time
argv = json.loads(sys.argv[1])
try:
    p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    rc, out, err = p.returncode, p.stdout, p.stderr
except subprocess.TimeoutExpired as exc:
    rc, out, err = 'hung', exc.stdout or '', exc.stderr or ''
    out = out.decode() if isinstance(out, bytes) else out
    err = err.decode() if isinstance(err, bytes) else err
time.sleep(0.5)
left = []
for e in pathlib.Path('/proc').iterdir():
    if e.name.isdigit() and int(e.name) != 1:
        try:
            st = (e / 'stat').read_text().split(') ', 1)[1][0]
            cmd = (e / 'cmdline').read_bytes().replace(b'\\0', b' ').decode()
        except OSError:
            continue
        if st != 'Z':
            left.append(cmd)
print(json.dumps({'rc': rc, 'stdout': out, 'stderr': err, 'left': left}))
"""


def _run_in_pidns(argv, env=None):
    proc = subprocess.run(
        [*PIDNS, sys.executable, "-c", IN_NS, json.dumps(argv)],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, **(env or {})},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(PIDNS is None, reason="cannot create a PID namespace")
def test_timed_out_runner_leaves_no_process_behind(tmp_path):
    # Live process test: runs inside its own PID namespace.
    repo, base = _repo(tmp_path)
    _commit(
        repo,
        {"test_calc.py": BASE_TESTS + "\n\ndef test_x():\n    assert clamp(-1) == 0\n"},
        "t",
    )
    cmd = "sh -c 'sleep 300' & sleep 300 # {tests} {junit}"
    out = _run_in_pidns(
        [
            sys.executable,
            str(SCRIPT),
            "--workdir",
            str(repo),
            "--json",
            "--base",
            base,
            "--test-cmd",
            cmd,
            "--timeout",
            "2",
        ]
    )
    assert out["rc"] != "hung" and out["left"] == [], out
    assert out["rc"] == 2, out
