"""Tests for red_check.py: new tests must go red for the RIGHT reason on the base.

Each planted case builds a real throwaway git repo (base commit -> head commit),
runs red_check.py as a subprocess with the real pytest runner, and asserts the
verdict for the planted test. The planted cases are the whole point: a check
that has never been shown to reject a bad test is a check that can't fail.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

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


def test_extra_assertion_type_is_honoured():
    c = red_check.classify_outcome
    assert c("failure", None, "ContractError: x") == "exception"
    assert c("failure", None, "ContractError: x", ("ContractError",)) == "assertion"
