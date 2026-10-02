"""The test session's sandbox fallback: only genuine absence of bubblewrap
turns the gate tests unisolated. A bad override or a bubblewrap that does not
isolate stops the session instead of silently running mutants unisolated."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _session(tmp_path: Path, env: dict) -> subprocess.CompletedProcess:
    d = tmp_path / "suite"
    d.mkdir()
    for f in ("conftest.py", "sandbox.py"):
        shutil.copy(HERE / f, d / f)
    (d / "test_probe.py").write_text(
        "import os\n\n\ndef test_mode():\n"
        "    print('MODE=' + os.environ.get('GOODFELLOW_SANDBOX', 'unset'))\n"
    )
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider", str(d)],
        cwd=d,
        capture_output=True,
        text=True,
        env=env,
    )


def _base_env() -> dict:
    return {
        k: v
        for k, v in os.environ.items()
        if k
        not in (
            "GOODFELLOW_SANDBOX",
            "GOODFELLOW_BWRAP",
            "GOODFELLOW_REQUIRE_SANDBOX_TESTS",
        )
    }


def test_bad_override_stops_the_session(tmp_path):
    p = _session(tmp_path, {**_base_env(), "GOODFELLOW_BWRAP": str(tmp_path / "typo")})
    assert p.returncode != 0, p.stdout
    assert "MODE=off" not in p.stdout
    assert "GOODFELLOW_BWRAP" in p.stdout + p.stderr


def test_broken_bwrap_stops_the_session(tmp_path):
    broken = tmp_path / "broken-bwrap"
    broken.write_text("#!/bin/sh\nexit 1\n")
    broken.chmod(0o755)
    p = _session(tmp_path, {**_base_env(), "GOODFELLOW_BWRAP": str(broken)})
    assert p.returncode != 0, p.stdout
    assert "MODE=off" not in p.stdout


def test_genuine_absence_runs_the_suite_unisolated(tmp_path):
    farm = tmp_path / "bin"
    farm.mkdir()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if os.path.isdir(d):
            for name in os.listdir(d):
                if name != "bwrap" and not (farm / name).exists():
                    (farm / name).symlink_to(Path(d) / name)
    p = _session(tmp_path, {**_base_env(), "PATH": str(farm)})
    assert p.returncode == 0, p.stdout + p.stderr
    assert "MODE=off" in p.stdout
