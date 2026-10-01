"""red_check and mutation_check run every test command in the sandbox, or refuse.

Fail-closed: when the sandbox is required (the default) and unavailable, the
gate exits 2 and the test command never runs. Opt-out: GOODFELLOW_SANDBOX=off
runs unisolated, says so on every run, and the report records it. Live: a test
inside the gate cannot write outside the gate's throwaway copy.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import sandbox

HERE = Path(__file__).resolve().parent
RED = HERE / "red_check.py"
MUT = HERE / "mutation_check.py"
REQUIRE = os.environ.get("GOODFELLOW_REQUIRE_SANDBOX_TESTS") == "1"


def _live_ok() -> bool:
    try:
        sandbox.create([], environ={**os.environ, "GOODFELLOW_SANDBOX": "bwrap"})
        return True
    except sandbox.SandboxError:
        if REQUIRE:
            raise
        return False


needs_live = pytest.mark.skipif(not _live_ok(), reason="bwrap cannot isolate here")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(repo: Path, files: dict, msg: str) -> str:
    for name, body in files.items():
        (repo / name).write_text(body)
        _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Path, test_body: str) -> tuple[Path, str]:
    """base: f() returns 1. branch: f() returns 2, plus a new test of it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    base = _commit(
        repo,
        {"gate.py": "def f():\n    return 1\n", ".gitignore": "__pycache__/\n"},
        "base",
    )
    _commit(
        repo,
        {
            "gate.py": "def f():\n    return 2\n",
            "test_gate.py": "from gate import f\n\n\ndef test_f():\n" + test_body,
            "hs.txt": "gate.py\n",
        },
        "change",
    )
    return repo, base


def _env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "GOODFELLOW_SANDBOX"}
    env.update(extra)
    return env


def _red(repo: Path, base: str, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(RED),
            "--workdir",
            str(repo),
            "--base",
            base,
            "--json",
            *args,
        ],
        capture_output=True,
        text=True,
        env=env,
    )


def _mut(repo: Path, base: str, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable, str(MUT), "--workdir", str(repo), "--base", base,
            "--paths-file", str(repo / "hs.txt"), "--workers", "1", "--json", *args,
        ],
        capture_output=True,
        text=True,
        env=env,
    )  # fmt: skip


# --- fail closed --------------------------------------------------------------


@pytest.mark.parametrize("gate", ["red", "mutation"])
def test_missing_sandbox_refuses_and_runs_nothing(tmp_path, gate):
    repo, base = _repo(tmp_path, "    assert f() == 2\n")
    ran = tmp_path / "test-command-ran"
    cmd = f"touch {ran}; exit 0"
    env = _env(GOODFELLOW_BWRAP=str(tmp_path / "no-bwrap-here"))
    if gate == "red":
        proc = _red(repo, base, env, "--test-cmd", cmd)
    else:
        proc = _mut(repo, base, env, "--test-cmd", cmd)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "bubblewrap" in proc.stderr and "GOODFELLOW_SANDBOX=off" in proc.stderr
    assert not ran.exists(), "the test command ran without a sandbox"


@pytest.mark.parametrize("gate", ["red", "mutation"])
def test_opt_out_runs_unisolated_and_says_so(tmp_path, gate):
    repo, base = _repo(tmp_path, "    assert f() == 2\n")
    env = _env(GOODFELLOW_SANDBOX="off")
    proc = _red(repo, base, env) if gate == "red" else _mut(repo, base, env)
    assert proc.returncode in (0, 1), proc.stdout + proc.stderr
    assert "UNISOLATED" in proc.stderr
    assert json.loads(proc.stdout)["sandbox"] == "off"


def test_mutation_signal_target_needs_isolation_only_without_the_sandbox(tmp_path):
    """A target that signals processes is refused unisolated (as before), and
    runs in the sandbox, where every test run has its own PID namespace."""
    repo, base = _repo(tmp_path, "    assert f() == 2\n")
    _commit(
        repo,
        {
            "gate.py": "import os\n\n\ndef f():\n    return 2\n\n\ndef stop(p):\n    os.kill(p, 9)\n"
        },
        "signals",
    )
    proc = _mut(repo, base, _env(GOODFELLOW_SANDBOX="off"))
    assert proc.returncode == 2 and "--isolated" in proc.stderr
    if _live_ok():
        proc = _mut(repo, base, _env())
        assert proc.returncode in (0, 1), proc.stderr
        data = json.loads(proc.stdout)
        assert data["sandbox"] == "bwrap" and data["pid_namespace"] is True


# --- live ---------------------------------------------------------------------

LEAKY = (
    "    try:\n"
    "        open({outside!r}, 'w').write('leak')\n"
    "    except OSError:\n"
    "        pass\n"
    "    assert f() == 2\n"
)


@needs_live
def test_red_check_tests_cannot_write_outside_the_copy(tmp_path):
    outside = tmp_path / "outside.txt"
    repo, base = _repo(tmp_path, LEAKY.format(outside=str(outside)))
    proc = _red(repo, base, _env())
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(proc.stdout)
    assert data["sandbox"] == "bwrap"
    assert [r["verdict"] for r in data["results"]] == ["OK"]
    assert not outside.exists(), "a test under red_check wrote to the host"


@needs_live
def test_mutation_check_tests_cannot_write_outside_the_copy(tmp_path):
    outside = tmp_path / "outside.txt"
    repo, base = _repo(tmp_path, LEAKY.format(outside=str(outside)))
    proc = _mut(repo, base, _env())
    data = json.loads(proc.stdout)
    assert data["sandbox"] == "bwrap", proc.stderr
    assert data["ran"] > 0
    assert not outside.exists(), "a test under mutation_check wrote to the host"
