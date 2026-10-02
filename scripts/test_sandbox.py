"""The gates run tests in a sandbox, or refuse to run them.

The pure tests pin the wrapper's shape and the fail-closed paths and run
everywhere. The live tests run real commands through bwrap; they skip where it
cannot run, unless GOODFELLOW_REQUIRE_SANDBOX_TESTS=1 (set in CI), where a
missing sandbox is a failure instead.
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
REQUIRE = os.environ.get("GOODFELLOW_REQUIRE_SANDBOX_TESTS") == "1"


def _live() -> sandbox.Sandbox | None:
    try:
        sb = sandbox.create([], environ={**os.environ, "GOODFELLOW_SANDBOX": "bwrap"})
    except sandbox.SandboxError as exc:
        if REQUIRE:
            raise
        print(f"sandbox unavailable: {exc}", file=sys.stderr)
        return None
    return sb


LIVE = _live()
needs_live = pytest.mark.skipif(LIVE is None, reason="bwrap cannot isolate here")


# --- configuration ----------------------------------------------------------


def test_sandbox_modes():
    assert sandbox.mode_from_env({}) == "auto"
    assert sandbox.mode_from_env({"GOODFELLOW_SANDBOX": "bwrap"}) == "bwrap"
    assert sandbox.mode_from_env({"GOODFELLOW_SANDBOX": "off"}) == "off"


def test_unknown_mode_fails_loudly():
    with pytest.raises(sandbox.SandboxError, match="not a valid value"):
        sandbox.mode_from_env({"GOODFELLOW_SANDBOX": "maybe"})


def test_missing_bwrap_refuses_with_a_way_out():
    with pytest.raises(sandbox.SandboxError) as exc:
        sandbox.create([], environ={"PATH": "/usr/bin"}, which=lambda _n: None)
    msg = str(exc.value)
    assert "bubblewrap" in msg and "GOODFELLOW_SANDBOX=off" in msg


def test_missing_bwrap_may_run_unsandboxed_only_when_the_caller_allows_it():
    sb = sandbox.create(
        [], environ={"PATH": "/usr/bin"}, which=lambda _n: None, allow_missing=True
    )
    assert sb.mode == "unsandboxed" and not sb.isolated
    # an explicit demand for the sandbox is never downgraded
    with pytest.raises(sandbox.SandboxError, match="bubblewrap"):
        sandbox.create(
            [],
            environ={"PATH": "/usr/bin", "GOODFELLOW_SANDBOX": "bwrap"},
            which=lambda _n: None,
            allow_missing=True,
        )


def test_present_but_broken_bwrap_is_never_downgraded(tmp_path):
    class Failed:
        returncode = 1
        stdout = ""
        stderr = "bwrap: setting up uid map: Permission denied"

    fake = tmp_path / "bwrap"
    fake.write_text("")
    with pytest.raises(sandbox.SandboxError, match="does not isolate"):
        sandbox.create(
            [],
            environ={"PATH": "/usr/bin", "GOODFELLOW_BWRAP": str(fake)},
            run=lambda *a, **k: Failed(),
            allow_missing=True,
        )


def test_a_probe_that_does_not_isolate_refuses(tmp_path):
    """bwrap is there but the probe sees the host: refuse, never run unisolated."""

    class Done:
        returncode = 0
        stdout = json.dumps(
            {"nprocs": 300, "driver_visible": True, "visible": [], "cwd_writable": True}
        )
        stderr = ""

    fake = tmp_path / "bwrap"
    fake.write_text("")
    with pytest.raises(sandbox.SandboxError, match="PID namespace"):
        sandbox.create(
            [],
            environ={"PATH": "/usr/bin", "GOODFELLOW_BWRAP": str(fake)},
            run=lambda *a, **k: Done(),
        )


def test_a_probe_write_that_reaches_the_host_refuses(tmp_path, monkeypatch):
    """The host's own view decides: if the probe's marker lands in a guarded
    directory on the host, the sandbox does not isolate, whatever it reports."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(sandbox, "_home", lambda: None)

    def leaky(cmd, **_k):
        marker = cmd[cmd.index("-c") + 2]
        (checkout / marker).write_text("x")

        class Done:
            returncode = 0
            stdout = json.dumps(
                {
                    "nprocs": 3,
                    "driver_visible": False,
                    "visible": [],
                    "cwd_writable": True,
                }
            )
            stderr = ""

        return Done()

    fake = tmp_path / "bwrap"
    fake.write_text("")
    with pytest.raises(sandbox.SandboxError, match="reached the host"):
        sandbox.create(
            [checkout],
            environ={"PATH": "/usr/bin", "GOODFELLOW_BWRAP": str(fake)},
            run=leaky,
        )
    assert list(checkout.iterdir()) == [], "the probe marker must be cleaned up"


def test_probe_verdicts():
    ok = {"nprocs": 3, "driver_visible": False, "visible": [], "cwd_writable": True}
    assert sandbox.probe_problems(0, json.dumps(ok)) == []
    assert sandbox.probe_problems(1, "bwrap: setting up uid map: Permission denied")
    assert sandbox.probe_problems(0, "not json")
    assert sandbox.probe_problems(0, json.dumps({**ok, "visible": ["/h/.ssh"]}))
    assert sandbox.probe_problems(0, json.dumps({**ok, "cwd_writable": False}))


def test_off_runs_the_command_unchanged():
    sb = sandbox.Sandbox(mode="off")
    assert sb.wrap("pytest -q", [Path("/w")], Path("/w")) == "pytest -q"


def test_wrapper_shape(tmp_path):
    sb = sandbox.Sandbox(mode="bwrap", bwrap="bwrap", ro=["/opt/py"])
    w = tmp_path / "w"
    argv = sb.argv([w], w)
    assert "--unshare-pid" in argv and "--die-with-parent" in argv
    pairs = list(zip(argv, argv[1:], argv[2:]))
    # never the host root, never writable outside the given directory
    assert ("--bind", "/", "/") not in pairs and ("--ro-bind", "/", "/") not in pairs
    binds = [(a, b) for a, b, _c in pairs if a == "--bind"]
    assert binds == [("--bind", str(w))]
    # the writable bind comes after every read-only mount and tmpfs
    last_ro = max(i for i, a in enumerate(argv) if a in ("--ro-bind", "--tmpfs"))
    assert argv.index("--bind") > last_ro
    assert ("--ro-bind", "/opt/py", "/opt/py") in pairs


def test_readonly_paths_never_include_root_or_home_ancestors(monkeypatch, tmp_path):
    home = tmp_path / "home" / "me"
    (home / "bin").mkdir(parents=True)
    monkeypatch.setattr(sandbox, "_home", lambda: home.resolve())
    env = {
        "PATH": os.pathsep.join(
            ["/", str(tmp_path / "home"), str(home), str(home / "bin")]
        )
    }
    ro = sandbox.readonly_paths(env)
    assert "/" not in ro
    assert str((tmp_path / "home").resolve()) not in ro
    assert str(home.resolve()) not in ro
    assert str((home / "bin").resolve()) in ro


def test_relative_extra_paths_are_rejected():
    with pytest.raises(sandbox.SandboxError, match="absolute"):
        sandbox.readonly_paths({"PATH": "", "GOODFELLOW_SANDBOX_RO": "rel/dir"})


# --- live -------------------------------------------------------------------


def _run(sb: sandbox.Sandbox, cmd: str, w: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        sb.wrap(cmd, [w], w), shell=True, capture_output=True, text=True, timeout=60
    )


@needs_live
def test_live_private_pid_namespace(tmp_path):
    p = _run(LIVE, "ls /proc | grep -c '^[0-9]'", tmp_path)
    assert p.returncode == 0, p.stderr
    assert int(p.stdout.strip()) <= sandbox.PROBE_MAX_PROCS


@needs_live
def test_live_writes_reach_only_the_given_directory(tmp_path):
    w = tmp_path / "w"
    w.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    home_marker = Path.home() / f".goodfellow-test-{os.getpid()}"
    cmd = (
        f"echo ok > inside.txt; echo leak > {outside}/leak.txt; "
        f"echo leak > {home_marker}; true"
    )
    p = _run(LIVE, cmd, w)
    assert p.returncode == 0, p.stderr
    assert (w / "inside.txt").read_text() == "ok\n"
    assert not (outside / "leak.txt").exists()
    assert not home_marker.exists()


@needs_live
def test_live_host_files_outside_the_allowlist_are_invisible(tmp_path):
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "token").write_text("s3cret")
    w = tmp_path / "w"
    w.mkdir()
    p = _run(LIVE, f"cat {secret}/token || echo hidden", w)
    assert "s3cret" not in p.stdout and "hidden" in p.stdout


@needs_live
def test_live_python_and_pytest_run(tmp_path):
    (tmp_path / "test_x.py").write_text("def test_x():\n    assert 1\n")
    p = _run(LIVE, f"{sys.executable} -m pytest -q -p no:cacheprovider", tmp_path)
    assert p.returncode == 0, p.stdout + p.stderr


# --- nothing that contains the checkout or home is mounted ------------------


def test_path_entries_exposing_a_guarded_dir_are_not_mounted(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "_home", lambda: None)
    checkout = tmp_path / "work" / "repo"
    (checkout / ".venv" / "bin").mkdir(parents=True)
    env = {
        "PATH": os.pathsep.join(
            [str(checkout), str(tmp_path / "work"), str(checkout / ".venv" / "bin")]
        )
    }
    ro = sandbox.readonly_paths(env, [checkout])
    assert str(checkout.resolve()) not in ro
    assert str((tmp_path / "work").resolve()) not in ro
    assert str((checkout / ".venv" / "bin").resolve()) in ro  # inside: only itself


def test_explicit_extra_that_exposes_the_checkout_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "_home", lambda: None)
    checkout = tmp_path / "repo"
    checkout.mkdir()
    with pytest.raises(sandbox.SandboxError, match="expose"):
        sandbox.readonly_paths(
            {"PATH": "", "GOODFELLOW_SANDBOX_RO": str(tmp_path)}, [checkout]
        )


def test_environment_credentials_are_dropped():
    env = {
        "PATH": "/usr/bin",
        "LC_ALL": "C.UTF-8",
        "GH_TOKEN": "canary",
        "AWS_SECRET_ACCESS_KEY": "canary",
        "MY_FIXTURE_DIR": "/data",
        "GOODFELLOW_SANDBOX_ENV": "MY_FIXTURE_DIR",
    }
    out = sandbox.sandbox_env(env)
    assert out["PATH"] == "/usr/bin" and out["LC_ALL"] == "C.UTF-8"
    assert "GH_TOKEN" not in out and "AWS_SECRET_ACCESS_KEY" not in out
    assert out["MY_FIXTURE_DIR"] == "/data"
    assert sandbox.Sandbox(mode="off").env(env) == env


@needs_live
def test_live_checkout_on_path_stays_invisible(tmp_path):
    checkout = tmp_path / "repo"
    checkout.mkdir()
    (checkout / "secret.env").write_text("TOKEN=s3cret")
    env = {**os.environ, "PATH": f"{checkout}{os.pathsep}{os.environ['PATH']}"}
    sb = sandbox.create([checkout], environ={**env, "GOODFELLOW_SANDBOX": "bwrap"})
    w = tmp_path / "w"
    w.mkdir()
    p = subprocess.run(
        sb.wrap(f"cat {checkout}/secret.env || echo hidden", [w], w),
        shell=True,
        capture_output=True,
        text=True,
        env=sb.env(env),
        timeout=60,
    )
    assert "s3cret" not in p.stdout and "hidden" in p.stdout


def test_completion_is_proven_by_the_inner_shell_only():
    sb = sandbox.Sandbox(mode="bwrap", bwrap="bwrap")
    assert not sb.completed("")
    assert not sb.completed("bwrap: setting up uid map: Permission denied")
    assert not sb.completed("goodfellow-sandbox-ran:someone-else:0")
    assert sb.completed(f"noise\ngoodfellow-sandbox-ran:{sb.nonce}:1\n")
    assert sandbox.Sandbox(mode="off").completed("")


@needs_live
def test_live_wrapped_command_reports_completion_and_keeps_its_exit_code(tmp_path):
    p = _run(LIVE, "exit 3", tmp_path)
    assert p.returncode == 3 and LIVE.completed(p.stderr)


def test_start_and_completion_are_distinct_markers():
    sb = sandbox.Sandbox(mode="bwrap", bwrap="bwrap")
    started_only = f"goodfellow-sandbox-ran:{sb.nonce}:start\n"
    assert sb.started(started_only) and not sb.completed(started_only)
    assert not sb.started("")
    assert sandbox.Sandbox(mode="off").started("")


def test_a_missing_bwrap_override_is_a_configuration_error(tmp_path):
    with pytest.raises(sandbox.SandboxError, match="GOODFELLOW_BWRAP") as exc:
        sandbox.create(
            [],
            environ={"PATH": "/usr/bin", "GOODFELLOW_BWRAP": str(tmp_path / "nope")},
            allow_missing=True,
        )
    assert not isinstance(exc.value, sandbox.SandboxMissing)
