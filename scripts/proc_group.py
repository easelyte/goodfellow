"""Run a shell command so that it, and everything it spawns, can be killed.

`subprocess.run(..., shell=True, timeout=...)` kills only the shell on timeout.
The test runner the shell started (and anything *it* started) is reparented to
init and keeps running, forever if the thing under test loops. For a tool that
deliberately runs code that may hang (a mutant with a flipped loop condition),
that leaks one busy process per timeout.

Here every command runs in its own session (a new process group). On timeout,
on normal exit (atexit) and on SIGTERM / SIGINT / SIGHUP, the whole group is
killed. `sweep_cwd` is a last line of defence before deleting a sandbox: it
stops processes whose working directory is still inside it (Linux /proc),
behind guards that do not depend on its own containment check (see its
docstring). Signalling code like this must only ever be tested against fakes
or inside its own PID namespace, and never mutation-tested on a live machine.
"""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Set, Tuple

MAX_SWEEP_KILLS = 20
_live: Dict[int, Optional[Path]] = {}  # pgid -> private cwd to sweep (or None)
_lock = threading.Lock()
_installed = False


def _kill_group(pgid: int, killpg=os.killpg, own_pgrp: Optional[int] = None) -> None:
    """SIGKILL a process group we created. Never group 0/1/negative (the
    caller's group, init, every process) nor our own group."""
    own = os.getpgrp() if own_pgrp is None else own_pgrp
    if pgid <= 1 or pgid == own:
        return
    try:
        killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def kill_all() -> None:
    with _lock:
        groups = list(_live.items())
        _live.clear()
    for pgid, sweep_root in groups:
        _kill_group(pgid)
        if sweep_root is not None:
            sweep_cwd(sweep_root)


def _on_signal(signum, _frame):
    kill_all()  # includes the sweeps, so nothing waits on the tool's finally
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
    sweep: bool = False,
) -> Tuple[Optional[int], str, str]:
    """Run `cmd` through the shell in its own process group.

    Returns (returncode, stdout, stderr); returncode is None on timeout. The
    group is always killed before returning, so nothing it started outlives it.
    With sweep=True, `cwd` must be a private directory (a sandbox): after the
    run, and on exit or a termination signal, any process still working inside
    it is killed too. That catches children that left the group (setsid).
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
            _live[pgid] = Path(cwd) if sweep else None
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
            if sweep:
                sweep_cwd(Path(cwd))
            with _lock:
                _live.pop(pgid, None)
        out_f.seek(0)
        err_f.seek(0)
        return rc, out_f.read(), err_f.read()


class ProcInfo(NamedTuple):
    pid: int
    ppid: int
    uid: int
    cwd: str  # realpath of the working directory ("" if unreadable)


def list_processes() -> List[ProcInfo]:
    """The real process table from /proc (empty where /proc is unavailable)."""
    out: List[ProcInfo] = []
    proc_dir = Path("/proc")
    if not proc_dir.is_dir():
        return out
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
            uid = entry.stat().st_uid
            cwd = os.readlink(entry / "cwd")
        except (OSError, ValueError, IndexError):
            continue
        if cwd.endswith(" (deleted)"):
            cwd = cwd[: -len(" (deleted)")]
        out.append(ProcInfo(int(entry.name), ppid, uid, os.path.realpath(cwd)))
    return out


def _inside(cwd: str, prefix: str) -> bool:
    """True when `cwd` is `prefix` or below it."""
    return cwd == prefix or cwd.startswith(prefix + os.sep)


def _ancestors(procs: List[ProcInfo], me: int) -> Set[int]:
    parent = {p.pid: p.ppid for p in procs}
    seen: Set[int] = set()
    cur = me
    while cur in parent and cur not in seen:
        seen.add(cur)
        cur = parent[cur]
    seen.add(cur)
    return seen


def sweep_cwd(
    root,
    *,
    lister: Optional[Callable[[], List[ProcInfo]]] = None,
    killer: Optional[Callable[[int, int], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
    temp_base: Optional[str] = None,
    max_kills: int = MAX_SWEEP_KILLS,
    grace: float = 0.5,
    me: Optional[int] = None,
    uid: Optional[int] = None,
) -> int:
    """Stop processes whose working directory is inside `root`, a private
    sandbox. Returns how many were signalled.

    The containment check is not trusted on its own. Independently of it:
      - `root` (resolved with realpath) must be strictly below the temp base,
        never the temp base itself, `/`, or empty; otherwise nothing is done;
      - a candidate's own cwd must also be strictly below the temp base;
      - pid 1, this process, its ancestors, and processes of other users are
        never signalled;
      - if more than `max_kills` processes qualify, it signals none (a sweep
        that wide means something is wrong, not that the sandbox is busy);
      - SIGTERM first, SIGKILL only for those still there after `grace`.
    """
    lister = lister or list_processes
    killer = killer or os.kill
    me = os.getpid() if me is None else me
    uid = os.getuid() if uid is None else uid
    base = os.path.realpath(temp_base or tempfile.gettempdir())
    prefix = os.path.realpath(str(root)) if str(root) else ""
    if not prefix or prefix == os.sep or not prefix.startswith(base + os.sep):
        print(f"proc_group: refusing to sweep {str(root)!r}", file=sys.stderr)
        return 0

    def targets() -> List[ProcInfo]:
        procs = lister()
        protected = _ancestors(procs, me) | {0, 1, me}
        return [
            p
            for p in procs
            if p.pid not in protected
            and p.uid == uid
            and p.cwd.startswith(base + os.sep)
            and _inside(p.cwd, prefix)
        ]

    first = targets()
    if len(first) > max_kills:
        print(
            f"proc_group: sweep of {prefix} matched {len(first)} processes "
            f"(cap {max_kills}); signalling none",
            file=sys.stderr,
        )
        return 0
    for p in first:
        try:
            killer(p.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    if first:
        sleep(grace)
        wanted = {p.pid for p in first}
        for p in targets():
            if p.pid in wanted:
                try:
                    killer(p.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    return len(first)
