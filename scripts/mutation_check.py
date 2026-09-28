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
    copy). Your working tree is never written.
  - Operators: comparison swap and boundary (`>=` to `>`), and/or swap, dropped
    `not`, negated `if`, flipped bool, int +1, arithmetic swap, `return X` to
    `return None`, `raise` to `pass` (fail-open), dropped call statement,
    break/continue swap.
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
baseline, no path list with --require-paths); 3 incomplete (time budget ran out
before every mutant ran, or a mutant run ended in a runner error such as
"command not found" rather than a test result; neither is a pass).
"""

from __future__ import annotations

import argparse
import ast
import concurrent.futures as cf
import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, Iterator, List, NamedTuple, Optional, Set, Tuple

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import proc_group  # noqa: E402

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
) -> str:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # never trust a stale .pyc of a mutant
    env["GIT_CEILING_DIRECTORIES"] = str(cwd.parent)
    if workdir is not None and env.get("PYTHONPATH"):
        env["PYTHONPATH"] = remap_pythonpath(env["PYTHONPATH"], workdir, cwd)
    rc, _out, _err = proc_group.run(cmd, cwd, env, timeout)
    if rc is None:
        return "timeout"
    return status_for_returncode(rc, error_codes)


def check(a: argparse.Namespace, workdir: Path) -> dict:
    changes = branch_changes(workdir, a.base)
    summary: dict = {
        "base": a.base,
        "targets": [],
        "out_of_scope": [],
        "unsupported": [],
        "mutants": 0,
        "ran": 0,
        "killed": 0,
        "skipped_budget": 0,
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
    for path in summary["targets"]:
        src = (workdir / path).read_text()
        sources[path] = src
        try:
            for m in enumerate_mutants(src, changes[path]):
                mutants.append((path, m))
        except SyntaxError as exc:
            raise CheckError(f"{path}: cannot parse ({exc})") from exc
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
        if run_tests(workers[0], cmd, a.baseline_timeout, workdir) != "survived":
            raise CheckError(
                "baseline is not green in the sandbox copy; fix the suite (or "
                "--test-cmd) before measuring mutants"
            )
        _reset_sandbox(workers[0], pristine, workdir)
        per_mutant = a.timeout or max(30, int((time.time() - t0) * 5) + 10)
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
                t1 = time.time()
                rec["status"] = run_tests(w, cmd, per_mutant, workdir, error_codes)
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
    finally:
        proc_group.kill_all()
        proc_group.sweep_cwd(tmp)  # anything still running inside a sandbox
        shutil.rmtree(tmp, ignore_errors=True)

    ran = [r for r in results if r["status"] not in ("skipped_budget", "error")]
    killed = [r for r in ran if r["status"] in ("killed", "timeout")]
    summary.update(
        {
            "ran": len(ran),
            "killed": len(killed),
            "skipped_budget": sum(r["status"] == "skipped_budget" for r in results),
            "runner_errors": sum(r["status"] == "error" for r in results),
            "score": round(len(killed) / len(ran), 3) if ran else None,
            "survivors": [r for r in ran if r["status"] == "survived"],
            "results": results,
        }
    )
    return summary


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
    ap.add_argument("--timeout", type=int, default=0, help="per-mutant seconds")
    ap.add_argument("--baseline-timeout", type=int, default=900)
    ap.add_argument(
        "--budget", type=int, default=900, help="total seconds for all mutants"
    )
    ap.add_argument(
        "--error-exit-codes",
        default=",".join(str(c) for c in sorted(RUNNER_ERROR_CODES)),
        help="test-command exit codes meaning 'could not run' (not a kill)",
    )
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    workdir = Path(a.workdir).resolve()
    a.workers = max(1, min(a.workers, os.cpu_count() or 1))
    if a.nice > 0 and hasattr(os, "nice"):
        os.nice(a.nice)  # inherited by every test run
    proc_group.install_handlers()

    try:
        s = check(a, workdir)
    except (CheckError, OSError) as exc:
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
        print(
            f"mutation-check: {s['killed']}/{s['ran']} mutants killed "
            f"({s['mutants']} generated, {s['skipped_budget']} not run) "
            f"in {len(s['targets'])} high-stakes file(s)"
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
