"""Tests for proc_group: bounded runs, and a sweep that cannot hit the machine.

Two kinds of test, kept strictly apart:

- **Fake-only** tests call `sweep_cwd` and `_kill_group` with an injected
  process lister and kill function. They never read the real /proc and never
  signal a real process, so they are safe to run anywhere, and safe to run
  under mutation (a mutant of the sweep cannot reach a real process).
- **Live** tests start and kill real processes. Each runs as a script inside
  its own PID namespace (`unshare --pid --fork --mount-proc`), where the only
  visible processes are the test's own. They are skipped where no namespace
  can be created.
"""

from __future__ import annotations

import ast
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

import proc_group as pg

TMP = "/tmp"


def P(pid, cwd, ppid=500, uid=1000):
    return pg.ProcInfo(pid=pid, ppid=ppid, uid=uid, cwd=cwd)


class FakeBox:
    """A fake process table and kill function. Killed processes disappear
    from later listings once they have received SIGKILL, or SIGTERM unless
    they are marked as ignoring it."""

    def __init__(self, procs, ignores_term=()):
        self.procs = {p.pid: p for p in procs}
        self.ignores_term = set(ignores_term)
        self.calls = []

    def lister(self):
        return list(self.procs.values())

    def killer(self, pid, sig):
        self.calls.append((pid, sig))
        if sig == signal.SIGKILL or pid not in self.ignores_term:
            self.procs.pop(pid, None)

    def killed(self):
        return sorted({pid for pid, _sig in self.calls})


# Our own pid is 900 (parent 800, grandparent 1), uid 1000.
ME = dict(me=900, uid=1000, temp_base=TMP, sleep=lambda _s: None)
SELF_CHAIN = [
    P(1, "/", ppid=0, uid=0),
    P(800, "/home/u", ppid=1),
    P(900, "/home/u", ppid=800),
]


def _sweep(box, root="/tmp/sbx", fn=None, **kw):
    fn = fn or pg.sweep_cwd
    return fn(root, lister=box.lister, killer=box.killer, **{**ME, **kw})


# --- fake-only: what the sweep kills ----------------------------------------


def test_kills_only_processes_inside_the_sandbox_term_then_kill():
    box = FakeBox(
        SELF_CHAIN
        + [
            P(10, "/tmp/sbx"),
            P(11, "/tmp/sbx/deep/dir"),
            P(12, "/tmp/sbx2"),  # sibling with a shared prefix
            P(13, "/tmp"),
            P(14, "/home/u/project"),
        ],
        ignores_term={11},
    )
    assert _sweep(box) == 2
    assert box.killed() == [10, 11]
    assert (10, signal.SIGTERM) in box.calls and (10, signal.SIGKILL) not in box.calls
    assert box.calls.index((11, signal.SIGTERM)) < box.calls.index((11, signal.SIGKILL))


def test_never_signals_pid_1_itself_its_ancestors_or_other_users():
    box = FakeBox(
        [
            P(1, "/tmp/sbx", ppid=0, uid=1000),
            P(800, "/tmp/sbx", ppid=1),
            P(900, "/tmp/sbx", ppid=800),
            P(20, "/tmp/sbx", uid=0),
            P(21, "/tmp/sbx", uid=1001),
            P(22, "/tmp/sbx"),
        ]
    )
    assert _sweep(box) == 1
    assert box.killed() == [22]


def test_refuses_roots_outside_the_temp_base():
    for root in ("", "/", "/tmp", "/home/u/project", "/tmpx/sbx", "/etc"):
        box = FakeBox(SELF_CHAIN + [P(30, "/"), P(31, "/tmp/sbx"), P(32, "/etc")])
        assert _sweep(box, root=root) == 0, root
        assert box.calls == [], root


def test_root_is_resolved_with_realpath(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to("/")
    box = FakeBox(SELF_CHAIN + [P(40, "/"), P(41, "/etc")])
    assert _sweep(box, root=str(link), temp_base=str(tmp_path)) == 0
    assert box.calls == []


def test_aborts_with_zero_kills_above_the_cap():
    box = FakeBox(SELF_CHAIN + [P(100 + i, "/tmp/sbx") for i in range(21)])
    assert _sweep(box, max_kills=20) == 0
    assert box.calls == []
    box = FakeBox(SELF_CHAIN + [P(100 + i, "/tmp/sbx") for i in range(20)])
    assert _sweep(box, max_kills=20) == 20


# --- fake-only: the exact incident mutant ----------------------------------


def _mutant_sweep():
    """sweep_cwd with the incident mutation applied: the inside-the-root check
    `cwd == prefix` flipped to `cwd != prefix`, which matched every process."""
    src = Path(pg.__file__).read_text()
    tree = ast.parse(src)
    flipped = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_inside":
            for cmp in ast.walk(node):
                if isinstance(cmp, ast.Compare) and isinstance(cmp.ops[0], ast.Eq):
                    cmp.ops[0] = ast.NotEq()
                    flipped += 1
    assert flipped == 1, "the containment check changed shape; update this test"
    mod = types.ModuleType("proc_group_mutant")
    exec(
        compile(ast.fix_missing_locations(tree), "proc_group_mutant", "exec"),
        mod.__dict__,
    )
    return mod


def test_incident_mutant_still_kills_nothing_outside_the_temp_base():
    mutant = _mutant_sweep()
    outside = [P(50, "/"), P(51, "/etc"), P(52, "/home/u/project"), P(53, "/var/lib/x")]
    box = FakeBox(SELF_CHAIN + outside + [P(54, "/tmp/other")])
    # Five matches would be under the cap; only the temp-base guard stands.
    _sweep(box, fn=mutant.sweep_cwd, max_kills=20)
    assert not {50, 51, 52, 53} & set(box.killed()), box.calls
    assert 1 not in box.killed() and 800 not in box.killed()


def test_incident_mutant_aborts_above_the_cap():
    mutant = _mutant_sweep()
    box = FakeBox(SELF_CHAIN + [P(200 + i, "/tmp/other-%d" % i) for i in range(30)])
    assert _sweep(box, fn=mutant.sweep_cwd, max_kills=20) == 0
    assert box.calls == []


# --- fake-only: the group kill ----------------------------------------------


def test_kill_group_never_targets_init_or_our_own_group():
    calls = []

    def fake_killpg(pgid, sig):
        calls.append((pgid, sig))

    for pgid in (0, 1, -1, 4242):
        pg._kill_group(pgid, killpg=fake_killpg, own_pgrp=4242)
    assert calls == []
    pg._kill_group(777, killpg=fake_killpg, own_pgrp=4242)
    assert calls == [(777, signal.SIGKILL)]


# --- live, inside a private PID namespace ----------------------------------


def _pidns_prefix():
    unshare = shutil.which("unshare")
    if not unshare:
        return None
    for extra in ([] if os.geteuid() == 0 else [], ["--user", "--map-root-user"]):
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
live = pytest.mark.skipif(PIDNS is None, reason="cannot create a PID namespace")

LIVE_PRELUDE = f"""
import os, signal, subprocess, sys, time, pathlib
sys.path.insert(0, {str(Path(pg.__file__).parent)!r})
import proc_group as pg
assert os.getpid() == 1, "live test is not inside its own PID namespace"
def others():
    out = []
    for e in pathlib.Path('/proc').iterdir():
        if e.name.isdigit() and int(e.name) != 1:
            try:
                st = (e / 'stat').read_text().split(') ', 1)[1][0]
            except OSError:
                continue
            if st != 'Z':
                out.append(int(e.name))
    return out
def settle():
    deadline = time.time() + 3
    while others() and time.time() < deadline:
        time.sleep(0.05)
    return others()
"""


def _live(body: str, tmp_path: Path):
    script = LIVE_PRELUDE + textwrap.dedent(body)
    proc = subprocess.run(
        [*PIDNS, sys.executable, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@live
def test_live_timeout_kills_the_shell_and_its_grandchildren(tmp_path):
    _live(
        """
        rc, _o, _e = pg.run("sleep 300 & sh -c 'sleep 300'", pathlib.Path('.'),
                            dict(os.environ), timeout=1)
        assert rc is None
        assert settle() == [], others()
        """,
        tmp_path,
    )


@live
def test_live_background_children_die_when_the_command_returns(tmp_path):
    _live(
        """
        rc, _o, _e = pg.run("(sleep 300 &) ; echo done", pathlib.Path('.'),
                            dict(os.environ), timeout=10)
        assert rc == 0
        assert settle() == [], others()
        """,
        tmp_path,
    )


@live
def test_live_detached_child_in_a_sandbox_dies_with_the_run(tmp_path):
    _live(
        """
        import tempfile
        box = pathlib.Path(tempfile.mkdtemp())
        rc, _o, _e = pg.run("setsid sleep 300 & sleep 300", box, dict(os.environ),
                            timeout=1, sweep=True)
        assert rc is None
        assert settle() == [], others()
        """,
        tmp_path,
    )


@live
def test_live_sigterm_to_the_tool_kills_its_running_groups(tmp_path):
    _live(
        f"""
        import tempfile
        box = tempfile.mkdtemp()
        tool = subprocess.Popen([sys.executable, '-c',
            "import os, sys, pathlib; sys.path.insert(0, {str(Path(pg.__file__).parent)!r});"
            "import proc_group; proc_group.run('setsid sleep 300 & sleep 300',"
            " pathlib.Path(%r), dict(os.environ), 600, sweep=True)" % box])
        deadline = time.time() + 5
        while len(others()) < 3 and time.time() < deadline:
            time.sleep(0.05)
        assert len(others()) >= 3, others()
        tool.send_signal(signal.SIGTERM)
        tool.wait(timeout=5)
        assert settle() == [], others()
        """,
        tmp_path,
    )
