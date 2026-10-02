#!/usr/bin/env python3
"""Diff-scoped mutation check for high-stakes Python paths.

A passing test suite says the code does what the tests check. It does not say the
tests would notice if the code were wrong. This gate answers the second question
for the code that matters most: it applies small deliberate breaks ("mutants") to
the lines your branch changed in high-stakes files, runs the tests against each,
and reports every mutant the tests fail to notice (a SURVIVOR).

  - Scope: only files matching your high-stakes path list, and only the lines the
    branch changed (for a pure deletion, the lines on either side of the gap) (Google's changed-lines approach: a handful of mutants per PR,
    not thousands).
  - Safety: mutants are applied in throwaway copies of the checkout (file modes
    kept; PYTHONPATH entries that point into the checkout are redirected to the
    copy). Your working tree is never written. Every test run happens in a
    sandbox (see sandbox.py): bubblewrap with a private PID namespace and a
    filesystem allowlist, where the only writable host directory is that run's
    copy. No sandbox means no run (exit 2); GOODFELLOW_SANDBOX=off runs the
    tests unisolated, knowingly, with a warning.
  - Operators: comparison swap and boundary (`>=` to `>`), and/or swap, dropped
    `not`, negated `if`, flipped bool, int +1, arithmetic swap, `return X` to
    `return None`, `raise` to `pass` (fail-open), dropped call statement,
    break/continue swap.
  - Refused: never mutation-test code that signals, deletes or writes real
    resources outside a fake or an isolated namespace. A mutant can turn "kill
    our child" into "kill every process on the machine", and a sandbox copy of
    the files does not contain that. A target that signals or spawns processes
    runs only with the sandbox (each run has its own PID namespace), or, with
    the sandbox off, with --isolated (the whole check runs inside a private
    PID namespace) or --fakes; a target that
    deletes or writes files needs --fakes (the tests replace those calls with
    fakes or temp directories), because a PID namespace does not protect the
    filesystem.
  - Time: each mutant gets at least three times the suite's runtime measured
    under the same parallel load (never under 30 s; --timeout overrides). A
    timeout counts as a kill only when its limit was at least 3x that loaded
    baseline; otherwise it is timeout_unverified and the result is incomplete.
  - Sampling: a NEW file (absent at the base) longer than 400 lines with more
    than 150 mutants is reduced to a fixed, seeded sample of 150 (--sample,
    --sample-min-lines, --no-sample), and the verdict says
    "(SAMPLED: file k/N)". Edits to existing files are never sampled.
  - Not mutated ("arid"): print and logging calls, `if __name__ == "__main__"`,
    `sys.path` edits, docstrings, and any line carrying `# pragma: no mutate`.

High-stakes path list resolution (first that exists wins):

  1. --paths-file <path>
  2. $GOODFELLOW_HIGH_STAKES_PATHS  (a file path)
  3. <workdir>/.goodfellow/high_stakes_paths.txt

Format: one glob per line, matched against the repo-relative path; `*` stays
within one directory, `**` spans any number of directories; `#` comments and
blank lines are ignored. Example: `src/auth/**`, `**/*_policy.py`, `billing.py`.

A surviving mutant is a finding: either add the test that kills it, or write down
why it is equivalent (the mutated program behaves identically). Mutants in files
other than Python are not generated; such files are listed as `unsupported`.

Exit codes: 0 every mutant killed (or nothing in scope, or no path list and not
--require-paths); 1 at least one survivor; 2 fail-closed (unknown base, red
baseline, no path list with --require-paths, a missing configured path list, or
a target with real side effects and no --isolated / --fakes, or --isolated
where no PID namespace can be created); 3 incomplete (time budget ran out
before every mutant ran, a mutant timed out under a limit too short to prove a
hang, or a mutant run ended in a runner error such as "command not found"
rather than a test result; none is a pass).
"""

from __future__ import annotations

import argparse
import ast
import concurrent.futures as cf
import copy
import hashlib
import json
import math
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Iterator, List, NamedTuple, Optional, Set, Tuple

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import proc_group  # noqa: E402
import sandbox  # noqa: E402

DEFAULT_TEST_CMD = (
    f"{shlex.quote(sys.executable)} -m pytest -x -q -p no:cacheprovider {{tests}}"
)
PRAGMA = "pragma: no mutate"
# Mutation runs are background work: stay well below the machine's capacity.
DEFAULT_WORKERS = max(1, min(4, (os.cpu_count() or 2) // 2))

CMP_SWAP = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.GtE,
    ast.GtE: ast.Lt,
    ast.Gt: ast.LtE,
    ast.LtE: ast.Gt,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
}
BOUNDARY = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt}
BIN_SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult}
LOG_METHODS = {
    "debug",
    "info",
    "warning",
    "warn",
    "error",
    "exception",
    "critical",
    "log",
}


class Site(NamedTuple):
    line: int
    kind: str
    call: str


# Calls that act on real resources outside the sandbox. A mutant of code that
# makes them can aim them at the wrong target: flip the "is this pid ours?"
# check and a cleanup routine kills every process on the machine. Such targets
# are refused unless the operator says the tests run against a fake or in an
# isolated namespace (--isolated).
_SIGNAL_CALLS = {"kill", "killpg", "pidfd_send_signal", "pthread_kill", "send_signal"}
_DELETE_CALLS = {"remove", "unlink", "rmdir", "removedirs", "rmtree"}
_PROCESS_CALLS = {
    "system",
    "popen",
    "Popen",
    "run",
    "call",
    "check_call",
    "check_output",
    "spawnv",
    "spawnl",
    "execv",
    "execvp",
    "execl",
    "execlp",
}
_WRITE_CALLS = {"write_text", "write_bytes", "rename", "replace", "move", "truncate"}
_PROCESS_MODULES = {"subprocess", "os", "asyncio"}


def _call_name(func: ast.AST) -> Tuple[str, str]:
    """(receiver, name) for a call target, e.g. ("os", "kill") or ("", "open")."""
    if isinstance(func, ast.Attribute):
        recv = func.value
        base = recv.id if isinstance(recv, ast.Name) else ""
        return base, func.attr
    if isinstance(func, ast.Name):
        return "", func.id
    return "", ""


def _open_writes(call: ast.Call) -> bool:
    """True for open() in a writing mode, or with a mode we cannot read."""
    mode_node: Optional[ast.AST] = call.args[1] if len(call.args) >= 2 else None
    for kw in call.keywords:
        if kw.arg == "mode":
            mode_node = kw.value
    if mode_node is None:
        return False  # default "r"
    if not (isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str)):
        return True  # unknown mode: fail closed
    return any(c in mode_node.value for c in "wax+")


_RISKY_MODULES = {"os", "signal", "subprocess", "shutil", "asyncio", "pty"}
_DYNAMIC_IMPORTS = {"__import__", "import_module"}


def side_effect_sites(src: str) -> List[Site]:
    """Call sites in `src` that signal processes, delete, write, or spawn.

    Aliases are resolved (`import os as o`, `from os import kill as k`), and
    anything this scan cannot see through is reported so the caller fails
    closed: getattr() on a risky module, dynamic imports and ctypes are
    "opaque" (they need --fakes), and open() with a non-literal mode is a
    write.
    """
    tree = ast.parse(src)
    module_alias: Dict[str, str] = {}  # local name -> module
    name_alias: Dict[str, Tuple[str, str]] = {}  # local name -> (module, name)
    sites: List[Site] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for al in node.names:
                top = al.name.split(".")[0]
                module_alias[al.asname or top] = top
                if top in ("ctypes", "multiprocessing"):
                    sites.append(Site(node.lineno, "opaque", f"import {al.name}"))
        elif isinstance(node, ast.ImportFrom) and node.module:
            top = node.module.split(".")[0]
            if top in ("ctypes", "multiprocessing"):
                sites.append(Site(node.lineno, "opaque", f"from {node.module} import"))
            for al in node.names:
                name_alias[al.asname or al.name] = (top, al.name)
                if al.name in _RISKY_MODULES:
                    module_alias[al.asname or al.name] = al.name

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        recv, name = _call_name(node.func)
        recv = module_alias.get(recv, recv)
        if not recv and name in name_alias:
            recv, name = name_alias[name]
        label = f"{recv}.{name}" if recv else name
        if name == "getattr" and node.args:
            target = node.args[0]
            if (
                isinstance(target, ast.Name)
                and module_alias.get(target.id) in _RISKY_MODULES
            ):
                sites.append(Site(node.lineno, "opaque", f"getattr({target.id}, ...)"))
            continue
        if name in _DYNAMIC_IMPORTS:
            sites.append(Site(node.lineno, "opaque", label))
            continue
        if name in _SIGNAL_CALLS:
            kind = "signal"
        elif name in _DELETE_CALLS:
            kind = "delete"
        elif name in _WRITE_CALLS or (name == "open" and _open_writes(node)):
            kind = "write"
        elif name in _PROCESS_CALLS and (recv in _PROCESS_MODULES or name == "Popen"):
            kind = "process"
        else:
            continue
        sites.append(Site(node.lineno, kind, label))
    return sorted(set(sites))


def isolation_suffices(kind: str) -> bool:
    """A PID namespace contains signals and spawned processes, nothing else."""
    return kind in ("signal", "process")


def needs_fakes(kind: str) -> bool:
    """Filesystem effects, and calls this scan cannot see through, need fakes."""
    return not isolation_suffices(kind)


class CheckError(RuntimeError):
    """Something prevented a trustworthy verdict: fail closed (exit 2)."""


class Mutant(NamedTuple):
    op: str
    line: int
    source: str


# --- path list ---------------------------------------------------------------


def parse_path_list(text: str) -> List[str]:
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def _glob_to_regex(pattern: str) -> "re.Pattern[str]":
    i, n, parts = 0, len(pattern), []
    while i < n:
        if pattern.startswith("**/", i):
            parts.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(parts) + "$")


def is_high_stakes(path: str, patterns: List[str]) -> bool:
    return any(_glob_to_regex(p).match(path) for p in patterns)


def resolve_paths_file(explicit: Optional[str], workdir: Path) -> Optional[Path]:
    if explicit:
        return Path(explicit)
    env = os.environ.get("GOODFELLOW_HIGH_STAKES_PATHS")
    if env:
        return Path(env)
    default = workdir / ".goodfellow" / "high_stakes_paths.txt"
    return default if default.exists() else None


# --- diff --------------------------------------------------------------------

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_lines(diff: str) -> Dict[str, Set[int]]:
    """{path: new-side line numbers} from a `git diff -U0` body."""
    out: Dict[str, Set[int]] = {}
    current: Optional[str] = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            current = target[2:] if target.startswith("b/") else None
            if current is not None:
                out.setdefault(current, set())
            continue
        m = _HUNK.match(line)
        if m and current is not None:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            if count == 0:
                # Pure deletion after new-side line `start`: measure the lines on
                # either side of the gap, so a removed guard is not invisible.
                out[current].update(n for n in (start, start + 1) if n > 0)
            else:
                out[current].update(range(start, start + count))
    return out


def _git(workdir: Path, args: List[str]) -> str:
    proc = subprocess.run(
        ["git", "-C", str(workdir), *args], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise CheckError(
            f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.returncode}"
        )
    return proc.stdout


def branch_changes(workdir: Path, base: str) -> Dict[str, Set[int]]:
    _git(workdir, ["rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"])
    merge_base = _git(workdir, ["merge-base", base, "HEAD"]).strip()
    diff = _git(
        workdir, ["diff", "-U0", "--no-color", "--no-ext-diff", merge_base, "--"]
    )
    changes = changed_lines(diff)
    for path in _git(
        workdir, ["ls-files", "--others", "--exclude-standard"]
    ).splitlines():
        p = workdir / path
        if p.is_file():
            n = len(p.read_text(errors="replace").splitlines())
            changes[path] = set(range(1, n + 1))
    return {k: v for k, v in changes.items() if v}


# --- mutants -----------------------------------------------------------------


def _is_arid_stmt(stmt: ast.stmt) -> bool:
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        f = stmt.value.func
        if isinstance(f, ast.Name) and f.id == "print":
            return True
        if isinstance(f, ast.Attribute):
            if f.attr in LOG_METHODS:
                return True
            if (
                isinstance(f.value, ast.Attribute)
                and f.value.attr == "path"
                and isinstance(f.value.value, ast.Name)
                and f.value.value.id == "sys"
            ):
                return True
    if isinstance(stmt, ast.If):
        t = stmt.test
        if (
            isinstance(t, ast.Compare)
            and isinstance(t.left, ast.Name)
            and t.left.id == "__name__"
        ):
            return True
        if "sys.path" in ast.unparse(t):
            return True
    return False


def _skipped_ids(tree: ast.AST) -> Set[int]:
    """ids of nodes never mutated: docstrings, f-strings, args, arid statements."""
    skip: Set[int] = set()

    def mark(node: ast.AST) -> None:
        for sub in ast.walk(node):
            skip.add(id(sub))

    for n in ast.walk(tree):
        if isinstance(
            n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            body = n.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                mark(body[0])
        if isinstance(n, (ast.JoinedStr, ast.arg)):
            mark(n)
        if isinstance(n, ast.stmt) and _is_arid_stmt(n):
            mark(n)
    return skip


def enumerate_mutants(src: str, lines: Optional[Set[int]]) -> Iterator[Mutant]:
    """Yield one Mutant per applicable operator on the given lines (None = all)."""
    tree = ast.parse(src)
    orig = ast.unparse(tree)
    # ast.unparse drops comments; keep the leading shebang / encoding lines so an
    # executable target still runs.
    header = ""
    for text in src.splitlines(keepends=True):
        if not text.startswith("#"):
            break
        header += text
    src_lines = src.splitlines()
    pragma_lines = {i + 1 for i, text in enumerate(src_lines) if PRAGMA in text}
    nodes = list(ast.walk(tree))
    skip = _skipped_ids(tree)

    candidates = []
    for i, n in enumerate(nodes):
        ln = getattr(n, "lineno", None)
        if ln is None or id(n) in skip or ln in pragma_lines:
            continue
        if lines is not None and ln not in lines:
            continue
        if isinstance(n, ast.Compare):
            for j, op in enumerate(n.ops):
                if type(op) in CMP_SWAP:
                    candidates.append((i, "cmp_swap", j))
                if type(op) in BOUNDARY:
                    candidates.append((i, "cmp_boundary", j))
        elif isinstance(n, ast.BoolOp):
            candidates.append((i, "boolop", None))
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            candidates.append((i, "drop_not", None))
        elif isinstance(n, ast.If):
            candidates.append((i, "negate_if", None))
        elif isinstance(n, ast.Constant) and isinstance(n.value, bool):
            candidates.append((i, "bool_const", None))
        elif isinstance(n, ast.Constant) and type(n.value) is int:
            candidates.append((i, "int_const", None))
        elif isinstance(n, ast.BinOp) and type(n.op) in BIN_SWAP:
            candidates.append((i, "binop", None))
        elif (
            isinstance(n, ast.Return)
            and n.value is not None
            and not (isinstance(n.value, ast.Constant) and n.value.value is None)
        ):
            candidates.append((i, "return_none", None))
        elif isinstance(n, ast.Raise):
            candidates.append((i, "raise_to_pass", None))
        elif isinstance(n, ast.Expr) and isinstance(n.value, ast.Call):
            candidates.append((i, "drop_call", None))
        elif isinstance(n, ast.Continue):
            candidates.append((i, "continue_to_break", None))
        elif isinstance(n, ast.Break):
            candidates.append((i, "break_to_continue", None))

    for i, kind, arg in candidates:
        t2 = copy.deepcopy(tree)
        n2 = list(ast.walk(t2))[i]
        repl: Optional[ast.AST] = None
        if kind == "cmp_swap":
            n2.ops[arg] = CMP_SWAP[type(n2.ops[arg])]()
        elif kind == "cmp_boundary":
            n2.ops[arg] = BOUNDARY[type(n2.ops[arg])]()
        elif kind == "boolop":
            n2.op = ast.Or() if isinstance(n2.op, ast.And) else ast.And()
        elif kind == "drop_not":
            repl = n2.operand
        elif kind == "negate_if":
            n2.test = ast.UnaryOp(op=ast.Not(), operand=n2.test)
        elif kind == "bool_const":
            n2.value = not n2.value
        elif kind == "int_const":
            n2.value = n2.value + 1
        elif kind == "binop":
            n2.op = BIN_SWAP[type(n2.op)]()
        elif kind == "return_none":
            n2.value = ast.Constant(value=None)
        elif kind in ("raise_to_pass", "drop_call"):
            repl = ast.Pass()
        elif kind == "continue_to_break":
            repl = ast.Break()
        elif kind == "break_to_continue":
            repl = ast.Continue()
        if repl is not None:
            for p in ast.walk(t2):
                for field, val in ast.iter_fields(p):
                    if isinstance(val, list):
                        for k, v in enumerate(val):
                            if v is n2:
                                val[k] = repl
                    elif val is n2:
                        setattr(p, field, repl)
        try:
            ast.fix_missing_locations(t2)
            new = ast.unparse(t2)
        except Exception:  # an operator produced an unprintable tree: skip it
            continue
        if new != orig:
            yield Mutant(kind, nodes[i].lineno, header + new)


# --- sandbox runs -------------------------------------------------------------


def _copy_tree(workdir: Path, dest: Path) -> None:
    listed = _git(
        workdir, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    )
    for rel in filter(None, listed.split("\0")):
        src = workdir / rel
        if not src.is_file() and not src.is_symlink():
            continue  # deleted in the working tree
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst, follow_symlinks=False)


# Exit codes that mean "the runner could not run the tests", not "a test failed":
# pytest internal error (3), usage error (4) and no tests collected (5), shell
# permission denied (126) and command not found (127). pytest's 2 (interrupted)
# stays a kill by default: it is how a mutant that breaks import at collection
# shows up, and `make` reports ordinary failures as 2. Override with
# --error-exit-codes. Counting these as kills would report a perfect
# score for mutants no test ever looked at.
RUNNER_ERROR_CODES = frozenset({3, 4, 5, 126, 127})


def status_for_returncode(rc: int, error_codes=RUNNER_ERROR_CODES) -> str:
    if rc == 0:
        return "survived"
    return "error" if rc in error_codes else "killed"


def remap_pythonpath(value: str, workdir: Path, sandbox: Path) -> str:
    """Point PYTHONPATH entries inside the real checkout at the sandbox copy, so
    the tests import the mutant and not the original."""
    out = []
    root = str(workdir)
    for entry in value.split(os.pathsep):
        if entry == root or entry.startswith(root + os.sep):
            entry = str(sandbox) + entry[len(root) :]
        out.append(entry)
    return os.pathsep.join(out)


def _manifest(root: Path) -> Dict[str, Tuple[int, int]]:
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            st = p.lstat()
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return out


def _reset_sandbox(
    w: Path, pristine: Dict[str, Tuple[int, int]], workdir: Path
) -> None:
    """Undo whatever a test run left behind: delete new files, restore changed
    ones from the checkout, so every mutant starts from the same state."""
    for dirpath, dirs, files in os.walk(w, topdown=False):
        for name in files:
            p = Path(dirpath) / name
            rel = str(p.relative_to(w))
            if rel not in pristine:
                p.unlink()
            else:
                st = p.lstat()
                if (st.st_size, st.st_mtime_ns) != pristine[rel]:
                    p.unlink()
                    shutil.copy2(workdir / rel, p, follow_symlinks=False)
        for name in dirs:
            d = Path(dirpath) / name
            if not d.is_symlink() and not any(d.iterdir()):
                d.rmdir()
    for rel in pristine:
        if not (w / rel).exists() and not (w / rel).is_symlink():
            (w / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(workdir / rel, w / rel, follow_symlinks=False)


def run_tests(
    cwd: Path,
    cmd: str,
    timeout: int,
    workdir: Optional[Path] = None,
    error_codes=RUNNER_ERROR_CODES,
    sb: Optional[sandbox.Sandbox] = None,
) -> str:
    if sb is not None:
        cmd = sb.wrap(cmd, [cwd], cwd)  # this run may write only its own copy
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # never trust a stale .pyc of a mutant
    env["GIT_CEILING_DIRECTORIES"] = str(cwd.parent)
    if workdir is not None and env.get("PYTHONPATH"):
        env["PYTHONPATH"] = remap_pythonpath(env["PYTHONPATH"], workdir, cwd)
    if sb is not None:
        env = sb.env(env)  # credentials in the environment stay outside
    rc, _out, err = proc_group.run(cmd, cwd, env, timeout, sweep=True)
    if rc is None:
        return "timeout"
    if sb is not None and not sb.completed(err):
        return "error"  # the sandbox failed, no test judged this run
    return status_for_returncode(rc, error_codes)


# --- time budget --------------------------------------------------------------

MIN_MUTANT_TIMEOUT = 30  # seconds; floor of the derived per-mutant timeout
DEFAULT_SAMPLE = 150  # mutants kept from a large NEW file
DEFAULT_SAMPLE_MIN_LINES = (
    400  # new files at or under this many lines are never sampled
)


def mutant_timeout(loaded_s: float, requested: Optional[int]) -> int:
    """Per-mutant timeout. An explicit --timeout wins; otherwise at least three
    times the baseline measured under the same parallel load (never below
    MIN_MUTANT_TIMEOUT), so a mutant that times out has provably hung instead of
    just running on a busy machine."""
    if requested:
        return int(requested)
    return max(MIN_MUTANT_TIMEOUT, math.ceil(3 * loaded_s))


def timeout_is_a_kill(loaded_s: float, limit_s: int) -> bool:
    """A timeout proves a hang only if the limit was at least 3x the loaded baseline."""
    return loaded_s * 3 <= limit_s


def recheck_timeouts(results: List[dict], loaded_s: float) -> float:
    """After the run: the loaded baseline is the slowest passing run seen (the
    calibration, or any surviving mutant's run under the same load). A timeout
    stays a kill only if ITS limit (which the budget may have cut down) is at
    least three times that; otherwise it becomes timeout_unverified, which makes
    the result incomplete. Returns the loaded baseline."""
    loaded = max(
        [loaded_s]
        + [r.get("secs", 0.0) for r in results if r.get("status") == "survived"]
    )
    for r in results:
        if r.get("status") == "timeout" and not timeout_is_a_kill(
            loaded, r.get("timeout_s", 0)
        ):
            r["status"] = "timeout_unverified"
    return loaded


def confirm_timeouts(
    results: List[dict], loaded_s: float, control: Callable[[], Tuple[str, float]]
) -> float:
    """Load can rise after calibration; if every mutant then times out, no
    survivor raises the baseline. So when any timeout would count as a kill,
    run the unmutated suite once more (`control`, returning (status, seconds))
    and recheck against the slower of the two. A control that does not pass
    leaves no baseline at all: every timeout becomes unverified."""
    if not any(r.get("status") == "timeout" for r in results):
        return loaded_s
    status, secs = control()
    if status != "survived":
        for r in results:
            if r.get("status") == "timeout":
                r["status"] = "timeout_unverified"
        return loaded_s
    return recheck_timeouts(results, max(loaded_s, secs))


def calibration_problem(statuses: List[str]) -> Optional[str]:
    """Why the unmutated suite is not green in every parallel sandbox, or None."""
    bad = sorted({s for s in statuses if s != "survived"})
    if not bad:
        return None
    return (
        f"the unmutated suite is not green when run in {len(statuses)} parallel "
        f"sandboxes ({', '.join(bad)}); fix the suite or pass --workers 1"
    )


def sample_mutants(
    path: str,
    text: str,
    is_new: bool,
    mutants: List[Mutant],
    *,
    k: int,
    min_lines: int,
) -> Tuple[List[Mutant], Optional[dict]]:
    """Keep a fixed, seeded sample of `k` mutants, but ONLY for bulk new code: a
    file absent at the base, with more than `min_lines` lines and more than `k`
    mutants. A file that exists at the base keeps every mutant on its changed
    lines, however much of it changed. The seed is the path plus the file's
    text, so the same tree always yields the same sample."""
    if not is_new or k <= 0 or len(text.splitlines()) <= min_lines or len(mutants) <= k:
        return mutants, None
    seed = hashlib.sha256(path.encode() + b"\0" + text.encode()).hexdigest()[:16]
    ordered = sorted(mutants, key=lambda m: (m.line, m.op, m.source))
    chosen = sorted(random.Random(seed).sample(range(len(ordered)), k))
    return [ordered[i] for i in chosen], {
        "population": len(mutants),
        "sampled": k,
        "seed": seed,
    }


def added_files(workdir: Path, base: str) -> Set[str]:
    """Files absent at the merge base: added on the branch, or untracked."""
    merge_base = _git(workdir, ["merge-base", base, "HEAD"]).strip()
    out = _git(workdir, ["diff", "--name-only", "--diff-filter=A", merge_base, "--"])
    out += _git(workdir, ["ls-files", "--others", "--exclude-standard"])
    return set(filter(None, out.splitlines()))


def check(a: argparse.Namespace, workdir: Path) -> dict:
    changes = branch_changes(workdir, a.base)
    summary: dict = {
        "base": a.base,
        "sandbox": "unused",
        "targets": [],
        "out_of_scope": [],
        "unsupported": [],
        "mutants": 0,
        "ran": 0,
        "killed": 0,
        "skipped_budget": 0,
        "timeout_unverified": 0,
        "runner_errors": 0,
        "score": None,
        "survivors": [],
        "results": [],
    }
    if a.files:
        patterns = list(a.files)
    else:
        pf = resolve_paths_file(a.paths_file, workdir)
        if pf is not None and not pf.exists():
            # An explicitly configured list that is missing is a typo, not an
            # opt-out: never let it turn the check into a silent skip.
            raise CheckError(f"high-stakes path list not found: {pf}")
        if pf is None:
            summary["skipped"] = (
                "no high-stakes path list (--paths-file / "
                "$GOODFELLOW_HIGH_STAKES_PATHS / .goodfellow/high_stakes_paths.txt)"
            )
            return summary
        patterns = parse_path_list(pf.read_text())

    for path in sorted(changes):
        hit = is_high_stakes(path, patterns)
        if not path.endswith(".py"):
            if hit:
                summary["unsupported"].append(path)
            continue
        if not hit:
            summary["out_of_scope"].append(path)
        elif (workdir / path).is_file():
            summary["targets"].append(path)

    mutants = []
    sources: Dict[str, str] = {}
    process_sites, fs_sites = [], []
    for path in summary["targets"]:
        try:
            for site in side_effect_sites((workdir / path).read_text()):
                line = f"{path}:{site.line} {site.kind} ({site.call})"
                (process_sites if isolation_suffices(site.kind) else fs_sites).append(
                    line
                )
        except SyntaxError as exc:
            raise CheckError(f"{path}: cannot parse ({exc})") from exc
    # Before any test runs: no sandbox, no run (never a fallback). A check
    # with nothing to mutate needs none.
    sb = sandbox.create([workdir]) if summary["targets"] else sandbox.Sandbox("off")
    summary["sandbox"] = sb.mode if summary["targets"] else "unused"
    if summary["targets"] and not sb.isolated:
        print(sandbox.unisolated_warning("mutation-check"), file=sys.stderr)
    problems = []
    if process_sites and not (sb.isolated or a.isolated or a.fakes):
        problems.append(
            "signals or spawns processes (a mutant can aim it at every process "
            "on the machine):\n  "
            + "\n  ".join(process_sites)
            + "\n  -> run with the sandbox (unset GOODFELLOW_SANDBOX), pass "
            "--isolated to run the check inside its own PID namespace (unshare "
            "--pid --fork --mount-proc), or use fakes and --fakes"
        )
    if fs_sites and not a.fakes:
        problems.append(
            "deletes or writes files, or makes calls this check cannot see "
            "through (a mutant can aim them at the real checkout or any path "
            "the user can write; a PID namespace does not contain that):\n  "
            + "\n  ".join(fs_sites)
            + "\n  -> pass --fakes only if the tests replace these calls with "
            "fakes or point them at temporary directories"
        )
    if problems:
        raise CheckError(
            "refusing to mutate code that acts on real resources. It "
            + "\nIt ".join(problems)
        )
    summary["pid_namespace"] = sb.isolated or os.getpid() == 1
    new = added_files(workdir, a.base) if summary["targets"] else set()
    sampled = []
    for path in summary["targets"]:
        src = (workdir / path).read_text()
        sources[path] = src
        try:
            found = list(enumerate_mutants(src, changes[path]))
        except SyntaxError as exc:
            raise CheckError(f"{path}: cannot parse ({exc})") from exc
        if not a.no_sample:
            found, info = sample_mutants(
                path, src, path in new, found, k=a.sample, min_lines=a.sample_min_lines
            )
            if info:
                sampled.append({"file": path, **info})
        mutants.extend((path, m) for m in found)
    if sampled:
        summary["sampled"] = sampled  # the verdict covers a seeded sample of these
    summary["mutants"] = len(mutants)
    if not mutants:
        return summary

    cmd = a.test_cmd.replace("{tests}", " ".join(shlex.quote(t) for t in a.tests))
    error_codes = frozenset(int(c) for c in a.error_exit_codes.split(",") if c.strip())
    tmp = Path(tempfile.mkdtemp(prefix="mutation-check-"))
    try:
        workers = []
        for k in range(max(1, a.workers)):
            w = tmp / f"w{k}"
            _copy_tree(workdir, w)
            workers.append(w)
        pristine = _manifest(workers[0])
        t0 = time.time()
        if run_tests(workers[0], cmd, a.baseline_timeout, workdir, sb=sb) != "survived":
            raise CheckError(
                "baseline is not green in the sandbox copy; fix the suite (or "
                "--test-cmd) before measuring mutants"
            )
        loaded = time.time() - t0
        summary["baseline_s"] = round(loaded, 1)
        _reset_sandbox(workers[0], pristine, workdir)
        if len(workers) > 1:
            # Calibrate under the load the mutants will see: the unmutated suite
            # in every sandbox at once. A timeout proves a hang only at >= 3x
            # THIS, so contention between workers can never become a kill.
            def calibrate(w: Path) -> Tuple[str, float]:
                t1 = time.time()
                st = run_tests(w, cmd, a.baseline_timeout, workdir, sb=sb)
                return st, time.time() - t1

            with cf.ThreadPoolExecutor(max_workers=len(workers)) as ex:
                calib = list(ex.map(calibrate, workers))
            for w in workers:
                _reset_sandbox(w, pristine, workdir)
            problem = calibration_problem([st for st, _s in calib])
            if problem:
                raise CheckError(problem)
            loaded = max([loaded] + [s for _st, s in calib])
        per_mutant = mutant_timeout(loaded, a.timeout)
        summary["mutant_timeout_s"] = per_mutant
        deadline = time.time() + a.budget
        free = list(workers)
        lock = threading.Lock()

        def job(item):
            path, m = item
            src_line = sources[path].splitlines()[m.line - 1].strip()
            rec = {"file": path, "line": m.line, "op": m.op, "src_line": src_line}
            if time.time() >= deadline:
                rec["status"] = "skipped_budget"
                return rec
            with lock:
                w = free.pop()
            target = w / path
            mode = target.stat().st_mode
            try:
                target.unlink()
                target.write_text(m.source)
                os.chmod(target, mode)
                # never run past the budget; a timeout under a cut-down limit
                # is then below 3x the loaded baseline and stays unverified
                rec["timeout_s"] = max(1, min(per_mutant, int(deadline - time.time())))
                t1 = time.time()
                rec["status"] = run_tests(
                    w, cmd, rec["timeout_s"], workdir, error_codes, sb
                )
                rec["secs"] = round(time.time() - t1, 1)
            finally:
                if target.exists():
                    target.unlink()
                target.write_text(sources[path])
                os.chmod(target, mode)
                _reset_sandbox(w, pristine, workdir)
                with lock:
                    free.append(w)
            return rec

        with cf.ThreadPoolExecutor(max_workers=len(workers)) as ex:
            results = list(ex.map(job, mutants))
        loaded = recheck_timeouts(results, loaded)

        def control() -> Tuple[str, float]:
            w = workers[0]
            _reset_sandbox(w, pristine, workdir)
            t1 = time.time()
            st = run_tests(w, cmd, a.baseline_timeout, workdir, sb=sb)
            return st, time.time() - t1

        summary["loaded_baseline_s"] = round(
            confirm_timeouts(results, loaded, control), 1
        )
    finally:
        proc_group.kill_all()
        proc_group.sweep_cwd(tmp)  # anything still running inside a sandbox
        shutil.rmtree(tmp, ignore_errors=True)

    ran = [
        r
        for r in results
        if r["status"] not in ("skipped_budget", "error", "timeout_unverified")
    ]
    killed = [r for r in ran if r["status"] in ("killed", "timeout")]
    summary.update(
        {
            "ran": len(ran),
            "killed": len(killed),
            "skipped_budget": sum(r["status"] == "skipped_budget" for r in results),
            "timeout_unverified": sum(
                r["status"] == "timeout_unverified" for r in results
            ),
            "runner_errors": sum(r["status"] == "error" for r in results),
            "score": round(len(killed) / len(ran), 3) if ran else None,
            "survivors": [r for r in ran if r["status"] == "survived"],
            "results": results,
        }
    )
    return summary


_IN_PIDNS = "GOODFELLOW_MUTATION_IN_PIDNS"


def _unshare_cmd() -> Optional[List[str]]:
    """An unshare prefix that yields a private PID namespace, or None."""
    unshare = os.environ.get("GOODFELLOW_UNSHARE") or shutil.which("unshare")
    if not unshare:
        return None
    variants = [[]] if os.geteuid() == 0 else []
    variants.append(["--user", "--map-root-user"])
    for extra in variants:
        cmd = [unshare, *extra, "--pid", "--fork", "--mount-proc"]
        try:
            probe = subprocess.run(
                [*cmd, "sh", "-c", "test $$ -eq 1"], capture_output=True, timeout=10
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return cmd
    return None


def _reexec_in_pid_namespace(argv: List[str]) -> int:
    cmd = _unshare_cmd()
    if cmd is None:
        print(
            "mutation-check BLOCK: --isolated, but cannot create a PID namespace "
            "(unshare missing or not permitted); refusing to run unisolated",
            file=sys.stderr,
        )
        return 2
    env = {**os.environ, _IN_PIDNS: "1"}
    return subprocess.run(
        [*cmd, sys.executable, str(Path(__file__).resolve()), *argv], env=env
    ).returncode


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", required=True, help="base ref (e.g. origin/main)")
    ap.add_argument("--workdir", default=".")
    ap.add_argument("--paths-file", default=None)
    ap.add_argument(
        "--files",
        nargs="+",
        default=None,
        help="glob(s) to mutate instead of the high-stakes path list",
    )
    ap.add_argument(
        "--require-paths",
        action="store_true",
        help="exit 2 if no high-stakes path list is configured",
    )
    ap.add_argument(
        "--test-cmd",
        default=DEFAULT_TEST_CMD,
        help="command run in the sandbox; {tests} is replaced by --tests",
    )
    ap.add_argument(
        "--tests", nargs="+", default=[], help="test files/ids (default: whole suite)"
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"parallel sandboxes (default {DEFAULT_WORKERS}; capped at the CPU count)",
    )
    ap.add_argument(
        "--nice",
        type=int,
        default=10,
        help="niceness added to this process and its test runs (0 to disable)",
    )
    ap.add_argument(
        "--timeout",
        type=int,
        default=0,
        help="per-mutant seconds (default: 3x the baseline measured under parallel "
        f"load, at least {MIN_MUTANT_TIMEOUT}; a timeout counts as a kill only "
        "when the limit is at least 3x that baseline)",
    )
    ap.add_argument(
        "--sample",
        type=int,
        default=DEFAULT_SAMPLE,
        help="mutants kept (fixed seed) from a NEW file longer than "
        "--sample-min-lines; edits to existing files are never sampled",
    )
    ap.add_argument("--sample-min-lines", type=int, default=DEFAULT_SAMPLE_MIN_LINES)
    ap.add_argument(
        "--no-sample", action="store_true", help="mutate every mutant of every target"
    )
    ap.add_argument("--baseline-timeout", type=int, default=900)
    ap.add_argument(
        "--budget", type=int, default=900, help="total seconds for all mutants"
    )
    ap.add_argument(
        "--error-exit-codes",
        default=",".join(str(c) for c in sorted(RUNNER_ERROR_CODES)),
        help="test-command exit codes meaning 'could not run' (not a kill)",
    )
    ap.add_argument(
        "--isolated",
        action="store_true",
        help="with GOODFELLOW_SANDBOX=off: re-run inside a private PID namespace "
        "(unshare --pid --fork --mount-proc), so mutants of code that signals or "
        "spawns processes can only see the check's own processes; fails closed if "
        "none can be made. With the sandbox (the default) every test run already "
        "has its own PID namespace, so this is not needed",
    )
    ap.add_argument(
        "--fakes",
        action="store_true",
        help="confirm the tests replace real side effects (deletes, writes, "
        "signals, spawns) with fakes or temp directories",
    )
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        unsandboxed = sandbox.mode_from_env() == "off"
    except sandbox.SandboxError as exc:
        print(f"mutation-check BLOCK: {exc}", file=sys.stderr)
        return 2
    reexec = a.isolated and unsandboxed
    if reexec and os.environ.get(_IN_PIDNS) != "1":
        return _reexec_in_pid_namespace(sys.argv[1:] if argv is None else argv)
    if reexec and os.getpid() != 1:
        print(
            "mutation-check BLOCK: --isolated but not in a PID namespace",
            file=sys.stderr,
        )
        return 2
    workdir = Path(a.workdir).resolve()
    a.workers = max(1, min(a.workers, os.cpu_count() or 1))
    if a.nice > 0 and hasattr(os, "nice"):
        os.nice(a.nice)  # inherited by every test run
    proc_group.install_handlers()

    try:
        s = check(a, workdir)
    except (CheckError, sandbox.SandboxError, OSError) as exc:
        print(f"mutation-check BLOCK: {exc}", file=sys.stderr)
        return 2

    if "skipped" in s:
        if a.require_paths:
            print(f"mutation-check BLOCK: {s['skipped']}", file=sys.stderr)
            return 2
        print(f"mutation-check SKIPPED: {s['skipped']}", file=sys.stderr)
    if a.json:
        print(json.dumps(s, indent=1))
    else:
        note = "".join(
            f" (SAMPLED: {x['file']} {x['sampled']}/{x['population']})"
            for x in s.get("sampled", [])
        )
        print(
            f"mutation-check: {s['killed']}/{s['ran']} mutants killed{note} "
            f"({s['mutants']} generated, {s['skipped_budget']} not run) "
            f"in {len(s['targets'])} high-stakes file(s)"
        )
        for x in s.get("sampled", []):
            print(
                f"  sampled {x['sampled']} of {x['population']} mutants in new file "
                f"{x['file']} (seed {x['seed']}); edits to existing files are never sampled"
            )
        for r in s["survivors"]:
            print(f"  SURVIVED {r['file']}:{r['line']} {r['op']:<18} {r['src_line']}")
        for path in s["unsupported"]:
            print(f"  not mutated (not Python): {path}")
    if s["survivors"]:
        print(
            f"mutation-check: {len(s['survivors'])} surviving mutant(s). Add a test "
            "that kills each, or record why it is equivalent.",
            file=sys.stderr,
        )
        return 1
    if s["runner_errors"]:
        print(
            f"mutation-check INCOMPLETE: {s['runner_errors']} mutant run(s) ended in a "
            "runner error (exit 4, 5, 126 or 127), so no test judged them.",
            file=sys.stderr,
        )
        return 3
    if s["timeout_unverified"]:
        print(
            f"mutation-check INCOMPLETE: {s['timeout_unverified']} mutant(s) timed out "
            "under a limit below 3x the loaded baseline, so the timeout does not prove "
            "a hang (raise --timeout or --budget, or lower --workers).",
            file=sys.stderr,
        )
        return 3
    if s["skipped_budget"]:
        print(
            "mutation-check INCOMPLETE: the time budget ran out before every mutant "
            "ran (raise --budget or narrow --tests).",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
