"""Run a shell command so that it, and everything it spawns, can be killed.

`subprocess.run(..., shell=True, timeout=...)` kills only the shell on timeout.
The test runner the shell started (and anything *it* started) is reparented to
init and keeps running, forever if the thing under test loops. For a tool that
deliberately runs code that may hang (a mutant with a flipped loop condition),
that leaks one busy process per timeout.

Here every command runs in its own session (a new process group). On timeout,
on normal exit (atexit) and on SIGTERM / SIGINT / SIGHUP, the whole group is
killed. `sweep_cwd` is a last line of defence before deleting a sandbox: it
kills any process whose working directory is still inside it (Linux /proc).
"""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

_live: Set[int] = set()
_lock = threading.Lock()
_installed = False


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def kill_all() -> None:
    with _lock:
        groups = list(_live)
        _live.clear()
    for pgid in groups:
        _kill_group(pgid)


def _on_signal(signum, _frame):
    kill_all()
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def install_handlers() -> None:
    """Kill every live group on exit and on termination signals (main thread)."""
    global _installed
    if _installed:
        return
    _installed = True
    atexit.register(kill_all)
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                signal.signal(sig, _on_signal)
            except (ValueError, OSError):
                pass


def run(
    cmd: str,
    cwd: Path,
    env: Dict[str, str],
    timeout: Optional[float],
) -> Tuple[Optional[int], str, str]:
    """Run `cmd` through the shell in its own process group.

    Returns (returncode, stdout, stderr); returncode is None on timeout. The
    group is always killed before returning, so nothing it started outlives it.
    """
    install_handlers()
    # Output goes to files, not pipes: a background child that inherited a pipe
    # would otherwise hold it open and make a finished command look hung.
    with tempfile.TemporaryFile("w+") as out_f, tempfile.TemporaryFile("w+") as err_f:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=str(cwd),
            env=env,
            stdout=out_f,
            stderr=err_f,
            stdin=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        pgid = proc.pid  # session leader: its pid is the group id
        with _lock:
            _live.add(pgid)
        try:
            try:
                rc: Optional[int] = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                rc = None
        finally:
            # Kill the group whether the command finished or timed out: nothing
            # it started (background children included) outlives the run.
            _kill_group(pgid)
            proc.wait()
            with _lock:
                _live.discard(pgid)
        out_f.seek(0)
        err_f.seek(0)
        return rc, out_f.read(), err_f.read()


def sweep_cwd(root: Path) -> int:
    """Kill processes whose cwd is inside `root`. Returns how many were killed.
    A no-op where /proc is unavailable."""
    proc_dir = Path("/proc")
    if not proc_dir.is_dir():
        return 0
    prefix = str(Path(root).resolve())
    killed = 0
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            cwd = os.readlink(entry / "cwd")
        except OSError:
            continue
        if cwd.endswith(" (deleted)"):
            cwd = cwd[: -len(" (deleted)")]
        if cwd == prefix or cwd.startswith(prefix + os.sep):
            try:
                os.kill(int(entry.name), signal.SIGKILL)
                killed += 1
            except (ProcessLookupError, PermissionError):
                pass
    return killed
