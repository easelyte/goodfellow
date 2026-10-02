#!/usr/bin/env python3
"""Run the gates' test commands in a sandbox, or not at all.

red_check and mutation_check run your tests, and mutation_check runs them
against deliberately broken code. A mutant of a cleanup routine can delete the
wrong directory; a mutant of process-selection code can signal the wrong
process. So every test command these gates run goes through bubblewrap
(`bwrap`) with:

  - a private PID namespace with its own /proc: the tests see and can signal
    only their own processes;
  - a filesystem ALLOWLIST: the system runtime (/usr and the /bin, /lib links),
    a short list of /etc files, the Python interpreter and its site-packages,
    the directories on PATH, and anything you add with GOODFELLOW_SANDBOX_RO,
    all read-only. /tmp, /var/tmp and /run are private and empty, and HOME is a
    private directory. The only host directory the tests can write is the
    gate's own throwaway copy. Your home directory, your checkout and your
    credentials are not mounted at all, and nothing that contains them is;
  - a minimal environment: credentials in environment variables are dropped.

Before the first test runs, a probe runs through the same wrapper and must
show a private PID namespace, no write reaching your home directory or your
checkout, and none of the usual credential locations visible. If bwrap is
missing or the probe fails, the gate refuses to run (exit 2). It never falls
back to running tests unisolated.

Configuration:

  GOODFELLOW_SANDBOX      unset or "bwrap": required (the default).
                          "off": run the tests unisolated, knowingly. Every
                          run prints a warning and the report says so. For
                          systems without bwrap (macOS, containers without
                          user namespaces).
  GOODFELLOW_SANDBOX_RO   extra read-only paths, separated by ":" (a toolchain
                          outside /usr, a shared fixture directory).
  GOODFELLOW_SANDBOX_ENV  extra environment variable names the tests keep,
                          comma-separated. By default a sandboxed test sees
                          only PATH, locale, terminal and Python variables:
                          tokens and keys in your environment are dropped.
  GOODFELLOW_BWRAP        path to the bwrap binary (default: found on PATH).

The network is not isolated: tests that need a local service keep working.
"""

from __future__ import annotations

import json
import os
import re
import pwd
import shlex
import shutil
import site
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence

MODE_ENV = "GOODFELLOW_SANDBOX"
RO_ENV = "GOODFELLOW_SANDBOX_RO"
BWRAP_ENV = "GOODFELLOW_BWRAP"

# /etc entries a test runtime needs (users, name resolution, TLS roots, the
# dynamic linker, Python and alternatives links, locale and zone data). Not
# /etc as a whole: it can hold credentials.
ETC_ALLOWLIST = tuple(
    f"/etc/{e}"
    for e in (
        "passwd", "group", "nsswitch.conf", "hosts", "host.conf", "resolv.conf",
        "gai.conf", "localtime", "timezone", "ssl", "ca-certificates",
        "ca-certificates.conf", "pki", "alternatives", "ld.so.cache", "ld.so.conf",
        "ld.so.conf.d", "python3", "mime.types", "magic", "os-release",
        "lsb-release", "debian_version", "locale.alias", "locale.conf",
        "protocols", "services", "shells", "terminfo", "inputrc", "fonts",
    )
)  # fmt: skip
# Top-level system directories: a symlink into /usr (merged-/usr systems) is
# recreated as a link, a real directory is bound read-only.
SYSTEM_DIRS = ("bin", "sbin", "lib", "lib32", "lib64", "libx32")
# Locations that commonly hold credentials. The probe requires every one that
# exists on the host to be invisible inside the sandbox.
SECRET_HOME_PATHS = (
    ".ssh", ".aws", ".gnupg", ".netrc", ".git-credentials", ".config/gh",
    ".docker", ".kube", ".pypirc", ".npmrc", ".claude", ".codex",
)  # fmt: skip

SANDBOX_HOME = "/tmp/home"


class SandboxError(RuntimeError):
    """No trustworthy sandbox: the gate must refuse to run tests (exit 2)."""


def mode_from_env(environ=os.environ) -> str:
    raw = environ.get(MODE_ENV, "").strip().lower()
    if raw in ("", "bwrap", "on", "1"):
        return "bwrap"
    if raw in ("off", "0", "none"):
        return "off"
    raise SandboxError(
        f"{MODE_ENV}={environ.get(MODE_ENV)!r} is not a valid value; use 'bwrap' "
        "(the default) or 'off'"
    )


def _home() -> Optional[Path]:
    try:
        return Path(pwd.getpwuid(os.geteuid()).pw_dir).resolve()
    except KeyError:
        return None


def _is_ancestor_or_same(a: Path, b: Path) -> bool:
    return a == b or a in b.parents


def readonly_paths(environ=os.environ, guarded: Sequence[Path] = ()) -> List[str]:
    """Host paths mounted read-only, besides /usr, the system links and /etc.

    Never / and never a directory that contains (or is) the home directory or a
    `guarded` one (the user's checkout): mounting it would expose every file in
    it, however private. A directory INSIDE one (a virtualenv in the checkout,
    ~/.local/bin) exposes only itself."""
    cands: List[str] = [
        sys.prefix,
        sys.base_prefix,
        sys.exec_prefix,
        str(Path(sys.executable).resolve().parent),
    ]
    try:
        cands += site.getsitepackages()
    except AttributeError:  # some virtualenv builds lack it
        pass
    try:
        cands.append(site.getusersitepackages())
    except AttributeError:
        pass
    cands += [p for p in environ.get("PATH", "").split(os.pathsep) if p]
    extra = [p for p in environ.get(RO_ENV, "").split(os.pathsep) if p]
    home = _home()
    protect = [Path("/")] + [Path(g).resolve() for g in guarded]
    if home is not None:
        protect.append(home)

    def exposes(real: Path) -> bool:
        return any(_is_ancestor_or_same(real, g) for g in protect)

    out: List[str] = []
    for c in cands:
        if not os.path.isabs(c) or not os.path.isdir(c):
            continue
        real = Path(c).resolve()
        if not exposes(real) and str(real) not in out:
            out.append(str(real))
    for c in extra:
        if not os.path.isabs(c):
            raise SandboxError(f"{RO_ENV} entries must be absolute paths: {c!r}")
        if not os.path.exists(c):
            continue
        real = Path(c).resolve()
        if exposes(real):
            raise SandboxError(
                f"{RO_ENV} entry {c!r} would expose the root, your home directory or "
                "the checkout; list the specific directories the tests need instead"
            )
        if str(real) not in out:
            out.append(str(real))
    return out


# Variables a sandboxed test command keeps; everything else (tokens, keys,
# session credentials) is dropped. GOODFELLOW_SANDBOX_ENV adds names.
ENV_ALLOWLIST = frozenset(
    {
        "PATH", "LANG", "LANGUAGE", "TZ", "TERM", "COLUMNS", "LINES", "CI",
        "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONHASHSEED",
        "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONWARNINGS", "VIRTUAL_ENV",
        "CONDA_PREFIX", "GIT_CEILING_DIRECTORIES", "NO_COLOR", "FORCE_COLOR",
    }
)  # fmt: skip
ENV_EXTRA = "GOODFELLOW_SANDBOX_ENV"


def sandbox_env(env: dict) -> dict:
    """The environment a sandboxed test command gets: the allowlist, LC_*, and
    the names in GOODFELLOW_SANDBOX_ENV (comma-separated)."""
    extra = {n.strip() for n in env.get(ENV_EXTRA, "").split(",") if n.strip()}
    keep = ENV_ALLOWLIST | extra
    return {k: v for k, v in env.items() if k in keep or k.startswith("LC_")}


@dataclass
class Sandbox:
    """How a gate wraps each test command. mode is 'bwrap' or 'off'."""

    mode: str
    bwrap: str = "bwrap"
    ro: List[str] = field(default_factory=list)
    # Printed on stderr by the shell INSIDE the sandbox after the test command
    # finishes. bwrap exits 1 when its own setup fails, the same code as a
    # failing test; without this line a broken sandbox would read as a failure
    # (a "killed" mutant, a red test).
    nonce: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def isolated(self) -> bool:
        return self.mode == "bwrap"

    def argv(self, writable: Sequence[Path], cwd: Path) -> List[str]:
        """The bwrap prefix for one run that may write only `writable`."""
        a = [
            self.bwrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--ro-bind", "/usr", "/usr",
        ]  # fmt: skip
        for d in SYSTEM_DIRS:
            p = Path("/") / d
            if p.is_symlink():
                a += ["--symlink", os.readlink(p), str(p)]
            elif p.is_dir():
                a += ["--ro-bind", str(p), str(p)]
        for e in ETC_ALLOWLIST:
            a += ["--ro-bind-try", e, e]
        a += [
            "--dev", "/dev",
            "--proc", "/proc",
            "--tmpfs", "/tmp",
            "--tmpfs", "/var/tmp",
            "--tmpfs", "/run",
            "--dir", SANDBOX_HOME,
            "--setenv", "HOME", SANDBOX_HOME,
            "--setenv", "TMPDIR", "/tmp",
        ]  # fmt: skip
        # Later mounts shadow earlier ones: read-only extras go after the
        # tmpfs mounts (an interpreter under /tmp stays visible), and the
        # writable directories go last.
        for p in self.ro:
            if p != "/usr" and not p.startswith("/usr/"):
                a += ["--ro-bind", p, p]
        for w in writable:
            a += ["--bind", str(w), str(w)]
        a += ["--chdir", str(cwd), "--"]
        return a

    def env(self, env: dict) -> dict:
        """The environment for a wrapped command: credentials dropped (see
        sandbox_env). Unchanged with mode 'off'."""
        return sandbox_env(env) if self.isolated else env

    def wrap(self, cmd: str, writable: Sequence[Path], cwd: Path) -> str:
        """A shell command line that runs `cmd` (itself a shell command) inside
        the sandbox. With mode 'off' it is `cmd` unchanged."""
        if not self.isolated:
            return cmd
        tag = self._tag()
        inner = (
            f'echo "{tag}:start" >&2; /bin/sh -c "$1"; rc=$?; '
            f'echo "{tag}:$rc" >&2; exit $rc'
        )
        return shlex.join(
            [*self.argv(writable, cwd), "/bin/sh", "-c", inner, "sh", cmd]
        )

    def _tag(self) -> str:
        return f"goodfellow-sandbox-ran:{self.nonce}"

    def started(self, stderr: str) -> bool:
        """Did the shell inside the sandbox start? A timeout without this is
        the sandbox stalling, not the tests: a runner error, never a kill."""
        return not self.isolated or f"{self._tag()}:start" in (stderr or "")

    def completed(self, stderr: str) -> bool:
        """Did the wrapped command run to its end inside the sandbox? Always
        True with mode 'off'. False means the sandbox itself failed: a runner
        error, never a test result."""
        if not self.isolated:
            return True
        return re.search(re.escape(self._tag()) + r":\d+\b", stderr or "") is not None


_PROBE = r"""
import json, os, sys
marker, driver_pid, *rest = sys.argv[1:]
n = int(rest[0]); canaries = rest[1:1 + n]; secrets = rest[1 + n:]
for d in canaries:  # the driver checks the HOST side for the marker afterwards
    try:
        with open(os.path.join(d, marker), "w") as fh:
            fh.write("x")
    except OSError:
        pass
pids = [p for p in os.listdir("/proc") if p.isdigit()]
print(json.dumps({
    "nprocs": len(pids),
    "driver_visible": os.path.exists("/proc/%s/cmdline" % driver_pid)
        and driver_pid not in ("1", "2", "3", "4"),
    "visible": [s for s in secrets if os.path.lexists(s)],
    "cwd_writable": os.access(os.getcwd(), os.W_OK),
}))
"""

PROBE_MAX_PROCS = 6


def probe_problems(rc: int, out: str) -> List[str]:
    """Pure verdict on the probe's output; an empty list means isolated."""
    if rc != 0:
        return [f"probe exited {rc}: {out.strip()[-300:]}"]
    try:
        data = json.loads(out.strip().splitlines()[-1])
        nprocs = int(data["nprocs"])
        driver = bool(data["driver_visible"])
        visible = list(data["visible"])
        cwd_ok = bool(data["cwd_writable"])
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        return [f"probe output unreadable ({exc!r}): {out.strip()[-300:]!r}"]
    problems = []
    if nprocs > PROBE_MAX_PROCS or driver:
        problems.append(f"no private PID namespace ({nprocs} processes visible)")
    if visible:
        problems.append(f"credential paths visible: {visible}")
    if not cwd_ok:
        problems.append("the sandbox's own directory is not writable")
    return problems


def _install_hint() -> str:
    return (
        "Install bubblewrap (Debian/Ubuntu: apt install bubblewrap; Fedora: dnf "
        "install bubblewrap; Arch: pacman -S bubblewrap). Where it cannot run "
        f"(macOS, a container without user namespaces), set {MODE_ENV}=off to run "
        "the gate's tests unisolated, knowingly."
    )


def create(
    canaries: Sequence[Path],
    *,
    environ=os.environ,
    which: Callable[[str], Optional[str]] = shutil.which,
    run=subprocess.run,
) -> Sandbox:
    """The sandbox for this gate run, after a probe proves it isolates.

    `canaries` are host directories no test may write (the user's checkout).
    Raises SandboxError when bwrap is required but missing or not isolating:
    there is no unisolated fallback."""
    mode = mode_from_env(environ)
    if mode == "off":
        return Sandbox(mode="off")
    bwrap = environ.get(BWRAP_ENV) or which("bwrap")
    if not bwrap or not os.path.exists(bwrap):
        raise SandboxError(
            "no test sandbox: bwrap (bubblewrap) not found. " + _install_hint()
        )
    home = _home()
    guarded = [Path(c).resolve() for c in canaries]
    if home is not None:
        guarded.append(home)
    guarded = [g for g in dict.fromkeys(guarded) if g.is_dir()]
    sb = Sandbox(mode="bwrap", bwrap=bwrap, ro=readonly_paths(environ, guarded))
    # Paths that must be invisible inside: the usual credential locations, and
    # a few real entries of each guarded directory (so a mount that exposes the
    # checkout or the home directory is caught by what the tests can read, not
    # only by what they can write). Entries holding a read-only mount are
    # skipped: their mounted part is visible by design.
    secrets = [str(home / s) for s in SECRET_HOME_PATHS] if home else []
    secrets = [s for s in secrets if os.path.lexists(s)]
    for g in guarded:
        try:
            names = sorted(os.listdir(g))
        except OSError:
            continue
        sample = [
            str(g / n)
            for n in names
            if not any(_is_ancestor_or_same(g / n, Path(r)) for r in sb.ro)
        ]
        secrets += [s for s in sample[:3] if s not in secrets]
    marker = f".goodfellow-sandbox-probe-{uuid.uuid4().hex}"
    with tempfile.TemporaryDirectory(prefix="goodfellow-probe-") as d:
        probe_dir = Path(d)
        cmd = sb.argv([probe_dir], probe_dir) + [
            sys.executable, "-c", _PROBE, marker, str(os.getpid()),
            str(len(guarded)), *map(str, guarded), *secrets,
        ]  # fmt: skip
        try:
            p = run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                env=sb.env(dict(environ)),
            )
            rc, out = (
                p.returncode,
                (p.stdout or "") + ((p.stderr or "") if p.returncode else ""),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            rc, out = 1, repr(exc)
    problems = probe_problems(rc, out)
    for g in guarded:  # the host's own view decides, not the probe's report
        leak = g / marker
        if leak.exists():
            leak.unlink()
            problems.append(f"a write reached the host: {g}")
    if problems:
        raise SandboxError(
            "the test sandbox (bwrap) does not isolate here; refusing to run tests: "
            + "; ".join(dict.fromkeys(problems))
            + ". "
            + _install_hint()
        )
    return sb


def unisolated_warning(tool: str) -> str:
    return (
        f"{tool}: WARNING {MODE_ENV}=off: the tests run UNISOLATED, with your "
        "processes and your files in reach. Use this only where bwrap cannot run."
    )
