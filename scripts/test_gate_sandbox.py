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


def test_red_check_without_bwrap_runs_unsandboxed_and_says_so_once(tmp_path):
    """Convenience by default: no bubblewrap (macOS) still gets a red check,
    with one warning line and an install hint, and the report says so."""
    repo, base = _repo(tmp_path, "    assert f() == 2\n")
    proc = _red(repo, base, _env(GOODFELLOW_BWRAP=str(tmp_path / "no-bwrap-here")))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(proc.stdout)
    assert data["sandbox"] == "unsandboxed"
    assert [r["verdict"] for r in data["results"]] == ["OK"]
    warn = [ln for ln in proc.stderr.splitlines() if "unsandboxed" in ln.lower()]
    assert len(warn) == 1 and "bubblewrap" in warn[0], proc.stderr


@pytest.mark.parametrize("extra", [[], ["--fakes"]])
def test_red_check_refuses_when_the_sandbox_is_demanded_or_broken(tmp_path, extra):
    repo, base = _repo(tmp_path, "    assert f() == 2\n")
    ran = tmp_path / "test-command-ran"
    cmd = f"touch {ran}; exit 0"
    broken = tmp_path / "broken-bwrap"
    broken.write_text(
        "#!/bin/sh\necho 'bwrap: setting up uid map: Permission denied' >&2\nexit 1\n"
    )
    broken.chmod(0o755)
    for env in (
        _env(GOODFELLOW_BWRAP=str(tmp_path / "none"), GOODFELLOW_SANDBOX="bwrap"),
        _env(GOODFELLOW_BWRAP=str(broken)),
    ):
        proc = _red(repo, base, env, "--test-cmd", cmd)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert not ran.exists(), "the test command ran without a working sandbox"


def test_mutation_check_without_bwrap_refuses_and_runs_nothing(tmp_path):
    """Strict where it matters: the mutation check runs deliberately broken
    code, so without the sandbox it needs --fakes or GOODFELLOW_SANDBOX=off."""
    repo, base = _repo(tmp_path, "    assert f() == 2\n")
    ran = tmp_path / "test-command-ran"
    cmd = f"touch {ran}; exit 0"
    proc = _mut(
        repo, base, _env(GOODFELLOW_BWRAP=str(tmp_path / "none")), "--test-cmd", cmd
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "bubblewrap" in proc.stderr
    assert "--fakes" in proc.stderr and "GOODFELLOW_SANDBOX=off" in proc.stderr
    assert not ran.exists(), "the test command ran without a sandbox"


def test_mutation_check_without_bwrap_runs_with_fakes(tmp_path):
    repo, base = _repo(tmp_path, "    assert f() == 2\n")
    proc = _mut(repo, base, _env(GOODFELLOW_BWRAP=str(tmp_path / "none")), "--fakes")
    assert proc.returncode in (0, 1), proc.stdout + proc.stderr
    data = json.loads(proc.stdout)
    assert data["sandbox"] == "unsandboxed" and data["ran"] > 0


def test_mutation_check_with_a_broken_bwrap_refuses_even_with_fakes(tmp_path):
    repo, base = _repo(tmp_path, "    assert f() == 2\n")
    ran = tmp_path / "test-command-ran"
    broken = tmp_path / "broken-bwrap"
    broken.write_text("#!/bin/sh\nexit 1\n")
    broken.chmod(0o755)
    proc = _mut(
        repo,
        base,
        _env(GOODFELLOW_BWRAP=str(broken)),
        "--fakes",
        "--test-cmd",
        f"touch {ran}",
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert not ran.exists()


@pytest.mark.parametrize("gate", ["red", "mutation"])
def test_opt_out_runs_unisolated_and_says_so(tmp_path, gate):
    repo, base = _repo(tmp_path, "    assert f() == 2\n")
    env = _env(GOODFELLOW_SANDBOX="off")
    proc = _red(repo, base, env) if gate == "red" else _mut(repo, base, env)
    assert proc.returncode in (0, 1), proc.stdout + proc.stderr
    assert "UNISOLATED" in proc.stderr
    assert json.loads(proc.stdout)["sandbox"] == "off"


def test_mutation_signal_target_still_needs_explicit_consent(tmp_path):
    """The sandbox isolates PIDs and files, not the network: a mutant of code
    that spawns `curl` can still reach a live service. So a target that signals
    or spawns processes needs --isolated or --fakes with or without it."""
    repo, base = _repo(tmp_path, "    assert f() == 2\n")
    _commit(
        repo,
        {
            "gate.py": "import os\n\n\ndef f():\n    return 2\n\n\ndef stop(p):\n    os.kill(p, 9)\n"
        },
        "signals",
    )
    proc = _mut(repo, base, _env(GOODFELLOW_SANDBOX="off"))
    assert proc.returncode == 2 and "--isolated" in proc.stderr, proc.stderr
    if _live_ok():  # without bwrap the default run stops earlier, at the sandbox
        proc = _mut(repo, base, _env())
        assert proc.returncode == 2 and "--isolated" in proc.stderr, proc.stderr
        proc = _mut(repo, base, _env(), "--isolated")
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


@needs_live
@pytest.mark.parametrize("gate", ["red", "mutation"])
def test_credentials_in_the_environment_do_not_reach_tests(tmp_path, gate):
    body = "    import os\n    assert os.environ.get('GH_TOKEN') is None\n    assert f() == 2\n"
    repo, base = _repo(tmp_path, body)
    env = _env(GH_TOKEN="canary-token")
    proc = _red(repo, base, env) if gate == "red" else _mut(repo, base, env)
    assert proc.returncode in (0, 1), proc.stdout + proc.stderr
    data = json.loads(proc.stdout)
    if gate == "red":
        assert [r["verdict"] for r in data["results"]] == ["OK"], data
    else:
        assert data["ran"] > 0, data


@needs_live
def test_red_check_tests_cannot_read_a_checkout_on_path(tmp_path):
    repo, base = _repo(tmp_path, "    assert f() == 2\n")
    (repo / "secret.env").write_text("TOKEN=s3cret")  # untracked, never copied
    body = (
        f"    import os\n    assert not os.path.exists({str(repo / 'secret.env')!r})\n"
        "    assert f() == 2\n"
    )
    _commit(
        repo, {"test_gate.py": "from gate import f\n\n\ndef test_f():\n" + body}, "read"
    )
    env = _env(PATH=f"{repo}{os.pathsep}{os.environ['PATH']}")
    proc = _red(repo, base, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert [r["verdict"] for r in json.loads(proc.stdout)["results"]] == ["OK"]


@needs_live
def test_a_sandbox_that_fails_after_the_probe_is_never_a_kill(tmp_path):
    """bwrap exits 1 when its own setup fails, the same code as a failing test.
    A wrapper failure after a good probe and baseline must make the result
    incomplete (runner error), never count every mutant as killed."""
    import shutil

    real = shutil.which("bwrap")
    count = tmp_path / "calls"
    fake = tmp_path / "flaky-bwrap"
    fake.write_text(
        "#!/bin/bash\n"
        f"n=$(cat {count} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {count}\n"
        f'[ "$n" -le 2 ] && exec {real} "$@"\n'
        'echo "bwrap: setting up uid map: Permission denied" >&2; exit 1\n'
    )
    fake.chmod(0o755)
    repo, base = _repo(tmp_path, "    assert f() == 2\n")
    proc = _mut(repo, base, _env(GOODFELLOW_BWRAP=str(fake)))
    data = json.loads(proc.stdout)
    assert data["killed"] == 0, data
    assert data["runner_errors"] == data["mutants"] > 0
    assert proc.returncode == 3, proc.stderr


@needs_live
def test_red_check_fails_closed_when_the_sandbox_breaks_on_the_base_run(tmp_path):
    import shutil

    real = shutil.which("bwrap")
    count = tmp_path / "calls"
    fake = tmp_path / "flaky-bwrap"
    fake.write_text(
        "#!/bin/bash\n"
        f"n=$(cat {count} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {count}\n"
        f'[ "$n" -le 2 ] && exec {real} "$@"\n'
        'echo "bwrap: setting up uid map: Permission denied" >&2; exit 1\n'
    )
    fake.chmod(0o755)
    repo, base = _repo(tmp_path, "    assert f() == 2\n")
    proc = _red(repo, base, _env(GOODFELLOW_BWRAP=str(fake)))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "sandbox" in proc.stderr


@needs_live
def test_a_sandbox_that_stalls_before_the_tests_start_is_never_a_kill(tmp_path):
    """A timeout proves a hang only if the tests started. bwrap stalling in its
    own setup is a runner error, never a killed mutant."""
    import shutil

    real = shutil.which("bwrap")
    count = tmp_path / "calls"
    fake = tmp_path / "stalling-bwrap"
    fake.write_text(
        "#!/bin/bash\n"
        f"n=$(cat {count} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {count}\n"
        f'[ "$n" -le 2 ] && exec {real} "$@"\n'
        "exec sleep 600\n"
    )
    fake.chmod(0o755)
    repo, base = _repo(tmp_path, "    assert f() == 2\n")
    proc = _mut(repo, base, _env(GOODFELLOW_BWRAP=str(fake)), "--timeout", "4")
    data = json.loads(proc.stdout)
    assert data["killed"] == 0, data
    assert data["runner_errors"] == data["mutants"] > 0
    assert proc.returncode == 3, proc.stderr
