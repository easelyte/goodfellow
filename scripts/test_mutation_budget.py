"""mutation_check's time budget: timeouts that prove a hang, and sampling bulk new code.

Two failure modes this pins down:

  - A mutant that times out is a kill only if the limit was at least three times
    the suite's runtime under the same parallel load. On a busy machine an
    unmutated run can be slow too; counting that slowness as a kill would report
    a perfect score for mutants no test ever caught.
  - A large NEW file can produce thousands of mutants. Those (and only those) are
    reduced to a fixed, seeded sample, and the verdict says so. An edit to an
    existing file is never sampled.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import mutation_check as mc

SCRIPT = Path(__file__).parent / "mutation_check.py"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _repo(tmp_path: Path, files: dict, paths: str) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "tester@example.com")
    _git(repo, "config", "user.name", "tester")
    (repo / "seed.txt").write_text("seed\n")
    (repo / ".gitignore").write_text("__pycache__/\n")
    _git(repo, "add", "seed.txt", ".gitignore")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    for name, body in files.items():
        (repo / name).write_text(body)
    (repo / "hs.txt").write_text(paths)
    _git(repo, "add", *files)
    _git(repo, "commit", "-q", "-m", "change")
    return repo, base


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--workdir",
            str(repo),
            "--paths-file",
            str(repo / "hs.txt"),
            "--workers",
            "2",
            *args,
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "GOODFELLOW_SANDBOX": "off"},
    )


# --- per-mutant timeout -----------------------------------------------------


def test_mutant_timeout_is_at_least_three_times_the_loaded_baseline():
    assert mc.mutant_timeout(20.0, None) == 60
    assert mc.mutant_timeout(20.2, None) == 61  # rounded up, never below 3x
    assert mc.mutant_timeout(2.0, None) == mc.MIN_MUTANT_TIMEOUT


def test_explicit_timeout_wins():
    assert mc.mutant_timeout(20.0, 7) == 7


def test_timeout_proves_a_hang_only_at_three_times_the_baseline():
    assert mc.timeout_is_a_kill(10.0, 30)
    assert not mc.timeout_is_a_kill(10.0, 29)


def test_timeouts_under_a_short_limit_become_unverified():
    results = [
        {"status": "timeout", "timeout_s": 5},  # 5 < 3 * 2.0 (slowest survivor)
        {"status": "timeout", "timeout_s": 30},
        {"status": "survived", "secs": 2.0},
        {"status": "killed", "secs": 9.0},  # a killed run's time says nothing
    ]
    loaded = mc.recheck_timeouts(results, 1.0)
    assert loaded == 2.0
    assert [r["status"] for r in results] == [
        "timeout_unverified",
        "timeout",
        "survived",
        "killed",
    ]


def test_calibration_must_be_green_in_every_worker():
    assert mc.calibration_problem(["survived", "survived"]) is None
    assert "timeout" in mc.calibration_problem(["survived", "timeout"])
    assert "killed" in mc.calibration_problem(["killed", "survived"])


SLOW_LOOP = "def countdown(n):\n    while n > 0:\n        n = n - 1\n    return n\n"
SLOW_TEST = (
    "import time\nfrom gate import countdown\n\n\n"
    "def test_c():\n    time.sleep(1.0)\n    assert countdown(3) == 0\n"
)


def test_a_timeout_below_three_times_the_baseline_is_incomplete(tmp_path):
    """The binop mutant hangs, but the explicit 2 s limit is under 3x the ~1 s
    suite, so the timeout proves nothing: incomplete (exit 3), never a kill."""
    repo, base = _repo(
        tmp_path, {"gate.py": SLOW_LOOP, "test_gate.py": SLOW_TEST}, "gate.py\n"
    )
    proc = _run(repo, "--base", base, "--timeout", "2", "--json")
    data = json.loads(proc.stdout)
    hang = [r for r in data["results"] if r["op"] == "binop"]
    assert hang and hang[0]["status"] == "timeout_unverified", data["results"]
    assert data["timeout_unverified"] >= 1
    assert data["loaded_baseline_s"] >= 1.0
    assert proc.returncode == 3, proc.stderr
    assert "INCOMPLETE" in proc.stderr


# --- sampling bulk new code -------------------------------------------------


def _big_module(n: int) -> str:
    return "".join(f"def f{i}(x):\n    return x > {i}\n\n\n" for i in range(n))


def _mutants(src: str) -> list:
    return list(mc.enumerate_mutants(src, None))


def test_large_new_file_is_sampled_reproducibly():
    src = _big_module(120)  # 480 lines
    muts = _mutants(src)
    assert len(muts) > 150
    a, info = mc.sample_mutants("big.py", src, True, muts, k=150, min_lines=400)
    b, _ = mc.sample_mutants(
        "big.py", src, True, list(reversed(muts)), k=150, min_lines=400
    )
    assert len(a) == 150 and a == b
    assert info["population"] == len(muts) and info["sampled"] == 150
    assert info["seed"]


def test_existing_file_is_never_sampled():
    src = _big_module(120)
    muts = _mutants(src)
    kept, info = mc.sample_mutants("big.py", src, False, muts, k=150, min_lines=400)
    assert kept == muts and info is None


def test_small_new_file_is_never_sampled():
    src = _big_module(90)  # 360 lines
    muts = _mutants(src)
    assert len(muts) > 150
    kept, info = mc.sample_mutants("small.py", src, True, muts, k=150, min_lines=400)
    assert kept == muts and info is None


def test_sampled_verdict_says_so(tmp_path):
    src = _big_module(110)  # 440 lines, new on the branch
    tests = "from big import f0\n\n\ndef test_f0():\n    assert f0(1) is True\n"
    repo, base = _repo(tmp_path, {"big.py": src, "test_big.py": tests}, "big.py\n")
    proc = _run(repo, "--base", base, "--sample", "4")
    population = len(_mutants(src))
    assert f"(SAMPLED: big.py 4/{population})" in proc.stdout, proc.stdout + proc.stderr
    data = json.loads(_run(repo, "--base", base, "--sample", "4", "--json").stdout)
    assert data["mutants"] == 4
    assert data["sampled"] == [
        {
            "file": "big.py",
            "population": population,
            "sampled": 4,
            "seed": data["sampled"][0]["seed"],
        }
    ]


def test_no_sample_mutates_everything(tmp_path):
    src = _big_module(110)
    tests = "from big import f0\n\n\ndef test_f0():\n    assert f0(1) is True\n"
    repo, base = _repo(tmp_path, {"big.py": src, "test_big.py": tests}, "big.py\n")
    data = json.loads(
        _run(repo, "--base", base, "--no-sample", "--budget", "0", "--json").stdout
    )
    assert data["mutants"] == len(_mutants(src))
    assert "sampled" not in data


def test_sampling_boundary_is_strictly_more_than_400_lines():
    assert (mc.DEFAULT_SAMPLE, mc.DEFAULT_SAMPLE_MIN_LINES) == (150, 400)
    exact = _big_module(100)  # exactly 400 lines
    assert len(exact.splitlines()) == 400
    muts = _mutants(exact)
    assert len(muts) > 150
    kept, info = mc.sample_mutants(
        "edge.py",
        exact,
        True,
        muts,
        k=mc.DEFAULT_SAMPLE,
        min_lines=mc.DEFAULT_SAMPLE_MIN_LINES,
    )
    assert kept == muts and info is None
    over = exact + "X = 1\n"  # 401 lines
    muts = _mutants(over)
    kept, info = mc.sample_mutants(
        "edge.py",
        over,
        True,
        muts,
        k=mc.DEFAULT_SAMPLE,
        min_lines=mc.DEFAULT_SAMPLE_MIN_LINES,
    )
    assert len(kept) == 150 and info["population"] == len(muts)


def test_timeouts_are_rechecked_against_a_fresh_control_run():
    """Load can rise after calibration. If every mutant then times out, no
    survivor raises the baseline, so a fresh unmutated control run decides."""
    calls = []

    def slow_control():
        calls.append(1)
        return "survived", 50.0  # the machine is now this slow

    results = [{"status": "timeout", "timeout_s": 60}, {"status": "killed", "secs": 1}]
    loaded = mc.confirm_timeouts(results, 10.0, slow_control)
    assert loaded == 50.0 and results[0]["status"] == "timeout_unverified"
    assert calls == [1]


def test_a_control_that_does_not_pass_leaves_every_timeout_unverified():
    results = [{"status": "timeout", "timeout_s": 600}]
    mc.confirm_timeouts(results, 1.0, lambda: ("timeout", 900.0))
    assert results[0]["status"] == "timeout_unverified"


def test_no_timeouts_no_control_run():
    results = [{"status": "killed", "secs": 1}]

    def boom():
        raise AssertionError("control must not run")

    assert mc.confirm_timeouts(results, 2.0, boom) == 2.0


def test_a_fast_control_keeps_the_timeout_a_kill():
    results = [{"status": "timeout", "timeout_s": 60}]
    mc.confirm_timeouts(results, 10.0, lambda: ("survived", 11.0))
    assert results[0]["status"] == "timeout"
