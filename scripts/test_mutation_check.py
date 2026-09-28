"""Tests for mutation_check.py: diff-scoped mutation testing of high-stakes paths.

The planted cases are the proof the gate can fail: a weak suite over a planted
boundary must leave a SURVIVING mutant (exit 1) and a strong suite must kill
every mutant (exit 0). Both run the real pytest runner in sandbox copies.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import mutation_check as mc

SCRIPT = Path(__file__).parent / "mutation_check.py"

LEGACY = "def legacy():\n    return 1 + 1\n"
GATE = (
    LEGACY
    + "\n\ndef allowed(n, limit=10):\n"
    + "    if n >= limit:\n"
    + "        return True\n"
    + "    return False\n"
)
WEAK_TESTS = "from gate import allowed\n\n\ndef test_big():\n    assert allowed(100)\n"
STRONG_TESTS = (
    "from gate import allowed, legacy\n\n\n"
    "def test_boundary():\n"
    "    assert allowed(10) is True\n"
    "    assert allowed(9) is False\n"
    "    assert allowed(100) is True\n"
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _write(repo: Path, files: dict) -> None:
    for name, body in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)


def _repo(tmp_path: Path, tests: str, paths: str = "gate.py\n") -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "tester@example.com")
    _git(repo, "config", "user.name", "tester")
    _write(repo, {"gate.py": LEGACY, ".gitignore": "__pycache__/\n"})
    _git(repo, "add", "gate.py", ".gitignore")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, {"gate.py": GATE, "test_gate.py": tests})
    if paths is not None:
        _write(repo, {"high_stakes_paths.txt": paths})
    _git(repo, "add", "gate.py", "test_gate.py")
    _git(repo, "commit", "-q", "-m", "add gate")
    return repo, base


def _run(
    repo: Path, *args: str, paths_file: bool = True, env: dict | None = None
) -> subprocess.CompletedProcess:
    extra = ["--paths-file", str(repo / "high_stakes_paths.txt")] if paths_file else []
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--workdir",
            str(repo),
            "--json",
            "--workers",
            "2",
            *extra,
            *args,
        ],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


# --- planted cases ----------------------------------------------------------


def test_weak_suite_leaves_boundary_survivor(tmp_path):
    repo, base = _repo(tmp_path, WEAK_TESTS)
    proc = _run(repo, "--base", base)
    data = json.loads(proc.stdout)
    survivors = {(s["file"], s["line"], s["op"]) for s in data["survivors"]}
    assert ("gate.py", 6, "cmp_boundary") in survivors, proc.stdout
    assert proc.returncode == 1


def test_strong_suite_kills_every_mutant(tmp_path):
    repo, base = _repo(tmp_path, STRONG_TESTS)
    proc = _run(repo, "--base", base)
    data = json.loads(proc.stdout)
    assert data["survivors"] == [], proc.stdout
    assert data["killed"] == data["mutants"] > 0
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_only_changed_lines_are_mutated(tmp_path):
    repo, base = _repo(tmp_path, STRONG_TESTS)
    data = json.loads(_run(repo, "--base", base).stdout)
    lines = {r["line"] for r in data["results"]}
    assert lines and lines <= {3, 4, 5, 6, 7, 8}, lines  # never legacy() on 1-2


def test_real_checkout_is_never_modified(tmp_path):
    repo, base = _repo(tmp_path, WEAK_TESTS)
    before = hashlib.sha256((repo / "gate.py").read_bytes()).hexdigest()
    _run(repo, "--base", base)
    assert hashlib.sha256((repo / "gate.py").read_bytes()).hexdigest() == before
    assert _git(repo, "status", "--porcelain") == "?? high_stakes_paths.txt"


# --- scoping, skip and fail-closed paths ------------------------------------


def test_file_outside_high_stakes_list_is_not_mutated(tmp_path):
    repo, base = _repo(tmp_path, WEAK_TESTS, paths="core/**\n")
    proc = _run(repo, "--base", base)
    data = json.loads(proc.stdout)
    assert data["out_of_scope"] == ["gate.py", "test_gate.py"]
    assert data["targets"] == []
    assert (data["mutants"], data["ran"], data["killed"]) == (0, 0, 0)
    assert proc.returncode == 0


def test_no_path_list_is_a_visible_skip_or_a_block_when_required(tmp_path):
    repo, base = _repo(tmp_path, WEAK_TESTS, paths=None)
    proc = _run(repo, "--base", base, paths_file=False)
    assert proc.returncode == 0
    assert "SKIPPED" in proc.stderr
    proc = _run(repo, "--base", base, "--require-paths", paths_file=False)
    assert proc.returncode == 2


def test_default_path_list_location_is_used(tmp_path):
    repo, base = _repo(tmp_path, WEAK_TESTS, paths=None)
    _write(repo, {".goodfellow/high_stakes_paths.txt": "gate.py\n"})
    proc = _run(repo, "--base", base, paths_file=False)
    assert json.loads(proc.stdout)["mutants"] > 0
    assert proc.returncode == 1


def test_red_baseline_fails_closed(tmp_path):
    repo, base = _repo(tmp_path, WEAK_TESTS + "\n\ndef test_red():\n    assert False\n")
    proc = _run(repo, "--base", base)
    assert proc.returncode == 2
    assert "baseline" in proc.stderr


def test_exhausted_budget_is_incomplete_not_clean(tmp_path):
    repo, base = _repo(tmp_path, STRONG_TESTS)
    proc = _run(repo, "--base", base, "--budget", "0")
    data = json.loads(proc.stdout)
    assert data["skipped_budget"] == data["mutants"] > 0
    assert proc.returncode == 3


def test_unknown_base_fails_closed(tmp_path):
    repo, _ = _repo(tmp_path, WEAK_TESTS)
    proc = _run(repo, "--base", "no-such-ref")
    assert proc.returncode == 2


def test_env_path_list_is_used(tmp_path):
    repo, base = _repo(tmp_path, WEAK_TESTS, paths=None)
    listing = tmp_path / "hs.txt"
    listing.write_text("gate.py\n")
    proc = _run(
        repo,
        "--base",
        base,
        paths_file=False,
        env={"GOODFELLOW_HIGH_STAKES_PATHS": str(listing)},
    )
    assert json.loads(proc.stdout)["targets"] == ["gate.py"]
    assert proc.returncode == 1


def test_untracked_high_stakes_file_is_mutated_whole(tmp_path):
    repo, base = _repo(tmp_path, STRONG_TESTS)
    _write(repo, {"extra.py": "def f(x):\n    return x > 1\n"})
    _write(repo, {"high_stakes_paths.txt": "gate.py\nextra.py\n"})
    data = json.loads(_run(repo, "--base", base).stdout)
    assert "extra.py" in data["targets"]
    assert {r["line"] for r in data["results"] if r["file"] == "extra.py"} == {2}


def test_non_python_high_stakes_file_is_listed_unsupported(tmp_path):
    repo, base = _repo(tmp_path, STRONG_TESTS, paths="gate.py\npolicy.sql\n")
    _write(repo, {"policy.sql": "select 1;\n"})
    data = json.loads(_run(repo, "--base", base).stdout)
    assert data["unsupported"] == ["policy.sql"]
    assert "policy.sql" not in data["targets"]


def test_unparseable_target_fails_closed(tmp_path):
    repo, base = _repo(tmp_path, STRONG_TESTS)
    _write(repo, {"gate.py": GATE + "\ndef broken(:\n"})
    proc = _run(repo, "--base", base)
    assert proc.returncode == 2
    assert "cannot parse" in proc.stderr


def test_mutant_that_hangs_counts_as_killed(tmp_path):
    loop = "def countdown(n):\n    while n > 0:\n        n = n - 1\n    return n\n"
    tests = (
        "from gate import countdown\n\n\ndef test_c():\n    assert countdown(3) == 0\n"
    )
    repo, base = _repo(tmp_path, tests)
    _write(repo, {"gate.py": loop})
    data = json.loads(_run(repo, "--base", base, "--timeout", "3").stdout)
    hang = [r for r in data["results"] if r["op"] == "binop"]
    assert hang and hang[0]["status"] == "timeout"
    assert data["killed"] == data["ran"]


def test_sandbox_temp_dirs_are_removed(tmp_path):
    repo, base = _repo(tmp_path, STRONG_TESTS)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _run(repo, "--base", base, env={"TMPDIR": str(scratch)})
    assert [
        p.name for p in scratch.iterdir() if p.name.startswith("mutation-check-")
    ] == []


def test_working_tree_deletion_does_not_stop_the_copy(tmp_path):
    # A tracked file deleted (uncommitted) in the working tree is skipped, and
    # every file after it is still copied into the sandbox.
    repo, base = _repo(tmp_path, STRONG_TESTS)
    _write(repo, {"a_first.py": "X = 1\n"})
    _git(repo, "add", "a_first.py")
    _git(repo, "commit", "-q", "-m", "a")
    (repo / "a_first.py").unlink()
    proc = _run(repo, "--base", base)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- units ------------------------------------------------------------------


def test_changed_lines_from_zero_context_diff():
    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        "@@ -3,0 +4,3 @@ def f():\n+a\n+b\n+c\n"
        "@@ -10 +13 @@\n-old\n+new\n"
        "@@ -20,2 +22,0 @@\n-gone\n-gone\n"
        "diff --git a/y.py b/y.py\nnew file mode 100644\n--- /dev/null\n+++ b/y.py\n"
        "@@ -0,0 +1,2 @@\n+p\n+q\n"
    )
    assert mc.changed_lines(diff) == {"x.py": {4, 5, 6, 13}, "y.py": {1, 2}}


def test_path_globs():
    pats = mc.parse_path_list("# comment\n\nsrc/auth/**\n**/*.sql\nbilling.py\n")
    assert pats == ["src/auth/**", "**/*.sql", "billing.py"]
    assert mc.is_high_stakes("src/auth/deep/token.py", pats)
    assert mc.is_high_stakes("src/auth/token.py", pats)
    assert mc.is_high_stakes("db/001.sql", pats)
    assert mc.is_high_stakes("top.sql", pats)
    assert mc.is_high_stakes("billing.py", pats)
    assert not mc.is_high_stakes("src/billing.py", pats)
    assert not mc.is_high_stakes("src/authz/token.py", pats)
    assert not mc.is_high_stakes("src/auth", pats[:1])
    assert mc.is_high_stakes("src/x/y.py", ["src/**.py"])
    assert not mc.is_high_stakes("src/x/y.pyc", ["src/**.py"])


def test_arid_and_pragma_lines_are_not_mutated():
    src = (
        "import logging\nimport sys\nlog = logging.getLogger(__name__)\n"
        "sys.path.insert(0, '.')\n"
        "def f(x):\n"
        "    print('checking', x)\n"
        "    log.info('x=%s', x)\n"
        "    if x > 1:  # pragma: no mutate\n"
        "        return True\n"
        "    return False\n"
        "def label(n):\n"
        "    return f'{n + 1}'\n"
        "if '.' not in sys.path:\n"
        "    sys.path.append('.')\n"
        "if __name__ == '__main__':\n"
        "    f(2)\n"
    )
    mutants = list(mc.enumerate_mutants(src, None))
    lines = {m.line for m in mutants}
    for arid in (4, 6, 7, 8, 13, 14, 15, 16):
        assert arid not in lines, f"line {arid} should not be mutated"
    assert {9, 10} <= lines
    # The return on line 12 is fair game; the arithmetic inside the f-string is not.
    assert {m.op for m in mutants if m.line == 12} == {"return_none"}


def test_operators_cover_boundary_and_fail_open_shapes():
    src = (
        "def g(x):\n"
        "    if x >= 3 and x != 7 and not x is None:\n"
        "        raise ValueError(x)\n"
        "    return True\n"
    )
    ops = {m.op for m in mc.enumerate_mutants(src, None)}
    assert {"cmp_boundary", "cmp_swap", "boolop", "negate_if", "raise_to_pass"} <= ops
    assert "drop_not" in ops
    assert {"bool_const", "return_none", "int_const"} <= ops
    for m in mc.enumerate_mutants(src, None):
        assert m.source != src
        compile(m.source, "g.py", "exec")


def test_executable_target_keeps_its_mode_in_the_sandbox(tmp_path):
    # The test command runs the target directly. Every mutant here is
    # behaviour-neutral, so all must SURVIVE; a mutant that lost its executable
    # bit would fail with "permission denied" and be miscounted as killed.
    repo, base = _repo(tmp_path, WEAK_TESTS)
    script = repo / "tool.py"
    script.write_text("#!/usr/bin/env python3\nflag = 5 >= 3\nprint('ok')\n")
    script.chmod(0o755)
    _git(repo, "add", "tool.py")
    _git(repo, "commit", "-q", "-m", "tool")
    _write(repo, {"high_stakes_paths.txt": "tool.py\n"})
    proc = _run(repo, "--base", base, "--test-cmd", "./tool.py")
    data = json.loads(proc.stdout)
    assert data["mutants"] > 0
    assert len(data["survivors"]) == data["mutants"], proc.stdout
    assert proc.returncode == 1


def test_mutants_keep_the_shebang_line():
    src = "#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\nflag = 5 >= 3\n"
    for m in mc.enumerate_mutants(src, None):
        assert m.source.startswith("#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n")


def test_runner_errors_are_not_kills():
    assert mc.status_for_returncode(0) == "survived"
    assert mc.status_for_returncode(1) == "killed"
    for rc in (4, 5, 126, 127):
        assert mc.status_for_returncode(rc) == "error"


def test_pythonpath_into_the_checkout_is_remapped_to_the_sandbox(tmp_path):
    repo, sandbox = tmp_path / "repo", tmp_path / "sb"
    value = os.pathsep.join([str(repo / "src"), "/elsewhere", "rel"])
    assert mc.remap_pythonpath(value, repo, sandbox) == os.pathsep.join(
        [str(sandbox / "src"), "/elsewhere", "rel"]
    )
    assert mc.remap_pythonpath(str(repo), repo, sandbox) == str(sandbox)
