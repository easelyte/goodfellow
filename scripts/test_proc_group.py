"""No process started through proc_group may outlive its run.

Each test starts `sleep <unique marker>` processes, some as grandchildren of the
shell, and then looks for that marker in /proc. A plain
`subprocess.run(shell=True, timeout=...)` fails these: it kills the shell and
leaves the grandchildren running.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import proc_group as pg

pytestmark = pytest.mark.skipif(
    not Path("/proc/self/cmdline").exists(), reason="needs /proc"
)


def _marker() -> str:
    return f"{400 + (time.time_ns() // 1000) % 100000 / 100000:.5f}"


def _alive(marker: str) -> list:
    hits = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            state = (entry / "stat").read_text().split(") ", 1)[1][0]
        except OSError:
            continue
        if marker.encode() in argv and state != "Z":
            hits.append(int(entry.name))
    return hits


def _gone(marker: str, wait: float = 3.0) -> bool:
    deadline = time.time() + wait
    while time.time() < deadline:
        if not _alive(marker):
            return True
        time.sleep(0.05)
    return False


def test_timeout_kills_the_shell_and_its_grandchildren(tmp_path):
    m = _marker()
    rc, _out, _err = pg.run(
        f"sleep {m} & sh -c 'sleep {m}'", tmp_path, dict(os.environ), timeout=1
    )
    assert rc is None
    assert _gone(m), f"left running: {_alive(m)}"


def test_background_children_die_when_the_command_returns(tmp_path):
    m = _marker()
    rc, _out, _err = pg.run(
        f"(sleep {m} &) ; echo done", tmp_path, dict(os.environ), timeout=10
    )
    assert rc == 0
    assert _gone(m), f"left running: {_alive(m)}"


def test_sweep_kills_processes_whose_cwd_is_inside_the_sandbox(tmp_path):
    m = _marker()
    inside = tmp_path / "w0"
    inside.mkdir()
    proc = subprocess.Popen(["sleep", m], cwd=inside, start_new_session=True)
    try:
        assert _alive(m)
        assert pg.sweep_cwd(tmp_path) >= 1
        proc.wait(timeout=3)
        assert _gone(m)
    finally:
        if proc.poll() is None:
            proc.kill()


def test_sigterm_to_the_tool_kills_its_running_groups(tmp_path):
    m = _marker()
    script = (
        "import os, sys, pathlib\n"
        f"sys.path.insert(0, {str(Path(pg.__file__).parent)!r})\n"
        "import proc_group\n"
        f"proc_group.run('sleep {m}', pathlib.Path('.'), dict(os.environ), 600)\n"
    )
    tool = subprocess.Popen([sys.executable, "-c", script], cwd=tmp_path)
    try:
        deadline = time.time() + 5
        while not _alive(m) and time.time() < deadline:
            time.sleep(0.05)
        assert _alive(m), "the tool never started its command"
        tool.send_signal(signal.SIGTERM)
        tool.wait(timeout=5)
        assert _gone(m), f"left running after SIGTERM: {_alive(m)}"
    finally:
        if tool.poll() is None:
            tool.kill()
