#!/usr/bin/env python3
"""Red check: every NEW test must fail on the base for the RIGHT reason, then pass.

A test written after the code, or one that goes red only because the symbol it
calls does not exist yet, proves nothing about the behaviour it claims to check.
This gate replays the branch's test files against the base revision and demands,
for every test that is new on the branch:

  base  -> a FAILURE raised by an assertion (assert, AssertionError, pytest.fail,
           "DID NOT RAISE"), not an ImportError / NameError / AttributeError /
           collection error, which only say "the code isn't there yet";
  head  -> a pass.

Verdicts per new test:
  OK            red on the base from an assertion, green on head
  WRONG_REASON  red on the base from a crash in code that exists there
                (TypeError, KeyError, ...), not from an assertion
  NEW_SYMBOL    red on the base only because the code under test is absent
                (ImportError, NameError, missing attribute). A replay cannot
                judge these; their evidence is the stub-first red recorded
                during development. Not a failure unless --strict.
  NOT_RED       already passes on the base: it does not detect the change
  NOT_GREEN     fails on head
  SKIPPED       skipped on head or base, so it never proved anything
  UNCLEAR       red on the base with a failure this tool cannot classify
                (no assertion type, no known assertion message); teach it
                with --assertion-type or --assertion-pattern

How it works (never touches your checkout): the test command runs in a
throwaway copy of your working tree, and in a temporary `git worktree` of the
base with the branch's changed test files (plus any --support files) copied
over it; the two JUnit XML reports are compared. Both are removed afterwards.

OK means the base failure came from an assertion. It does not prove it was the
assertion you meant: the base message is printed next to each verdict, so
compare it with the expected red the plan named.

Runner-agnostic via JUnit XML. The default command is pytest; for other runners
pass --test-cmd with `{tests}` and `{junit}` placeholders, e.g.
  --test-cmd "npx vitest run {tests} --reporter=junit --outputFile={junit}"

"New" means a test id (classname::name) present on the branch but absent from
the base's own version of the same files. A test whose body changed but whose
name did not is not re-checked; rename it or pass --all-tests.

Exit codes:
  0  no WRONG_REASON / NOT_RED / NOT_GREEN / SKIPPED / UNCLEAR verdict
     (NEW_SYMBOL is reported, and counts as bad under --strict; finding no new
     tests passes unless --require-tests)
  1  at least one bad verdict
  2  fail-closed: unknown base, no report (or an empty one) on head,
     duplicate test ids, git failure
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

DEFAULT_TEST_CMD = (
    f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider "
    "--continue-on-collection-errors {tests} --junitxml={junit}"
)
DEFAULT_TEST_GLOBS = (
    "test_*.py",
    "*_test.py",
    "*.test.js",
    "*.test.ts",
    "*.test.mjs",
    "*.test.tsx",
    "*.spec.js",
    "*.spec.ts",
    "*.spec.tsx",
)

# Failure types / message prefixes that mean "an assertion said the behaviour is
# wrong". Anything else that names an exception class means the test crashed.
ASSERTION_TYPES = ("AssertionError", "AssertError", "Failed", "ExpectationFailed")
_ASSERT_PREFIX = re.compile(r"^(assert\b|Failed:|DID NOT RAISE)")
_EXPECT_CALL = re.compile(r"\bexpect\(")
_EXC_PREFIX = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt))\b")


_MISSING_SYMBOL = re.compile(
    r"(ImportError|ModuleNotFoundError|NameError|cannot import name|"
    r"No module named|has no attribute|is not defined)"
)


def is_missing_symbol(text: str) -> bool:
    """True when a base-side failure only says the code under test is absent."""
    return bool(_MISSING_SYMBOL.search(text or ""))


class RedCheckError(RuntimeError):
    """Something prevented a trustworthy verdict: fail closed (exit 2)."""


def verdict_for(head_kind: str, base_kind: str, base_message: str) -> str:
    """One new test's verdict from its head outcome and its base outcome."""
    if head_kind == "skipped":
        return "SKIPPED"
    if head_kind != "pass":
        return "NOT_GREEN"
    if base_kind == "pass":
        return "NOT_RED"
    if base_kind == "assertion":
        return "OK"
    if base_kind == "skipped":
        return "SKIPPED"
    if base_kind == "unknown":
        return "UNCLEAR"
    if is_missing_symbol(base_message):
        return "NEW_SYMBOL"
    return "WRONG_REASON"


def classify_outcome(
    tag: str,
    typ: Optional[str],
    message: Optional[str],
    extra_assertion_types: Sequence[str] = (),
    patterns: Sequence[str] = (),
) -> str:
    """Map one JUnit outcome to pass / skipped / assertion / exception."""
    if tag == "pass":
        return "pass"
    if tag == "skipped":
        return "skipped"
    if tag == "error":
        return "exception"
    allowed = set(ASSERTION_TYPES) | set(extra_assertion_types)
    msg = (message or "").strip()
    if typ:
        short = typ.split(".")[-1]
        if short in allowed:
            return "assertion"
    if _ASSERT_PREFIX.match(msg):
        return "assertion"
    if any(re.search(p, msg) for p in patterns):
        return "assertion"
    m = _EXC_PREFIX.match(msg)
    if m:
        return "assertion" if m.group(1).split(".")[-1] in allowed else "exception"
    if typ and _EXC_PREFIX.match(typ.split(".")[-1]):
        return "exception"
    if _EXPECT_CALL.search(msg):
        return "assertion"  # Jest / Vitest style expect(...) matcher failure
    return "unknown"


def _git(workdir: Path, args: List[str], check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(workdir), *args], capture_output=True, text=True
    )
    if check and proc.returncode != 0:
        raise RedCheckError(
            f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.returncode}"
        )
    return proc.stdout


def changed_test_files(
    workdir: Path, merge_base: str, globs: Sequence[str]
) -> List[str]:
    """Test files added or modified since merge_base (committed or not)."""
    out = _git(
        workdir, ["diff", "--name-only", "--diff-filter=AM", merge_base]
    ).splitlines()
    out += _git(workdir, ["ls-files", "--others", "--exclude-standard"]).splitlines()
    seen = []
    for path in out:
        name = Path(path).name
        if any(fnmatch.fnmatch(name, g) for g in globs) and path not in seen:
            if (workdir / path).is_file():
                seen.append(path)
    return seen


def _copy_worktree(workdir: Path, dest: Path) -> None:
    """Copy tracked and untracked-but-not-ignored files (the working-tree state)."""
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


def parse_junit(path: Path) -> Dict[str, Tuple[str, Optional[str], str]]:
    """{test_id: (tag, type, message)} from a JUnit XML report."""
    results: Dict[str, Tuple[str, Optional[str], str]] = {}
    root = ET.parse(path).getroot()
    for tc in root.iter("testcase"):
        cls = tc.get("classname") or ""
        name = tc.get("name") or ""
        tid = f"{cls}::{name}" if cls else name
        tag, typ, msg = "pass", None, ""
        for child in tc:
            if child.tag in ("failure", "error", "skipped"):
                tag, typ = child.tag, child.get("type")
                msg = child.get("message") or ""
                detail = child.text or ""
                if not msg:
                    msg = detail
                elif detail and tag == "error":
                    msg = f"{msg}: {detail}"
                break
        if tid in results:
            raise RedCheckError(
                f"duplicate test id {tid!r} in {path.name}; cannot tell the "
                "cases apart (give them distinct names or classnames)"
            )
        results[tid] = (tag, typ, msg)
    return results


def run_tests(
    cwd: Path,
    tests: List[str],
    cmd_template: str,
    timeout: int,
    returncodes: Optional[List[int]] = None,
) -> Optional[Dict[str, Tuple[str, Optional[str], str]]]:
    """Run the test command in cwd; return parsed JUnit, or None if no report.
    The runner's exit code is appended to `returncodes` when given."""
    fd, junit = tempfile.mkstemp(prefix="red-check-", suffix=".xml")
    os.close(fd)
    os.unlink(junit)
    cmd = cmd_template.replace(
        "{tests}", " ".join(shlex.quote(t) for t in tests)
    ).replace("{junit}", shlex.quote(junit))
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return None
    if returncodes is not None:
        returncodes.append(proc.returncode)
    try:
        if not os.path.exists(junit) or os.path.getsize(junit) == 0:
            return None
        return parse_junit(Path(junit))
    except ET.ParseError:
        return None
    finally:
        if os.path.exists(junit):
            os.unlink(junit)


def _collection_errors(results: Dict[str, Tuple[str, Optional[str], str]]) -> str:
    msgs = [m for (tag, _t, m) in results.values() if tag == "error" and m]
    return "; ".join(sorted(set(msgs)))[:300]


def check(
    workdir: Path,
    base: str,
    tests: Optional[List[str]],
    support: List[str],
    cmd_template: str,
    timeout: int,
    globs: Sequence[str],
    extra_assertion_types: Sequence[str],
    all_tests: bool,
    assertion_patterns: Sequence[str] = (),
) -> Tuple[List[dict], List[str]]:
    _git(workdir, ["rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"])
    merge_base = _git(workdir, ["merge-base", base, "HEAD"]).strip()
    files = tests if tests else changed_test_files(workdir, merge_base, globs)
    if not files:
        return [], []

    tmp = Path(tempfile.mkdtemp(prefix="red-check-wt-"))
    wt = tmp / "base"
    try:
        # Head side: a throwaway copy of the working tree (committed or not),
        # so tests that write files never touch the real checkout.
        head_copy = tmp / "head"
        _copy_worktree(workdir, head_copy)
        head_rc: List[int] = []
        head = run_tests(head_copy, files, cmd_template, timeout, head_rc)
        if head and head_rc and head_rc[0] != 0:
            if all(tag in ("pass", "skipped") for tag, _t, _m in head.values()):
                raise RedCheckError(
                    f"the test command exited {head_rc[0]} on the current checkout "
                    "but its report shows no failing test; the report is partial, "
                    "so no verdict is trustworthy"
                )
        if not head:
            raise RedCheckError(
                "the test command produced no JUnit report (or one with no test "
                "cases) on the current checkout; cannot judge anything "
                "(check --test-cmd)"
            )
        _git(workdir, ["worktree", "add", "--detach", "--quiet", str(wt), merge_base])
        # The base's OWN tests identify which head tests are new.
        own = [f for f in files if (wt / f).is_file()]
        base_own = run_tests(wt, own, cmd_template, timeout) if own else {}
        base_own = base_own or {}
        for rel in list(files) + list(support):
            src = workdir / rel
            if src.is_file():
                dst = wt / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        base_run = run_tests(wt, files, cmd_template, timeout) or {}
    finally:
        _git(workdir, ["worktree", "remove", "--force", str(wt)], check=False)
        shutil.rmtree(tmp, ignore_errors=True)
        _git(workdir, ["worktree", "prune"], check=False)

    collection_note = _collection_errors(base_run)
    results: List[dict] = []
    unreplayed: List[str] = []
    for tid, (htag, htyp, hmsg) in head.items():
        if not all_tests and tid in base_own:
            unreplayed.append(tid)
            continue
        head_kind = classify_outcome(
            htag, htyp, hmsg, extra_assertion_types, assertion_patterns
        )
        if tid in base_run:
            btag, btyp, bmsg = base_run[tid]
            base_kind = classify_outcome(
                btag, btyp, bmsg, extra_assertion_types, assertion_patterns
            )
        else:
            base_kind, bmsg = (
                "exception",
                (
                    f"not collected on base: {collection_note}"
                    if collection_note
                    else "not run on base"
                ),
            )
        verdict = verdict_for(head_kind, base_kind, bmsg)
        results.append(
            {
                "test": tid,
                "verdict": verdict,
                "base": base_kind,
                "base_message": (bmsg or "").strip().splitlines()[0][:200]
                if (bmsg or "").strip()
                else "",
                "head": head_kind,
            }
        )
    results.sort(key=lambda r: r["test"])
    return results, sorted(unreplayed)


BAD = {"WRONG_REASON", "NOT_RED", "NOT_GREEN", "SKIPPED", "UNCLEAR"}
STRICT_BAD = BAD | {"NEW_SYMBOL"}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", required=True, help="base ref (e.g. origin/main)")
    ap.add_argument("--workdir", default=".", help="repo working directory")
    ap.add_argument(
        "--tests",
        nargs="+",
        default=None,
        help="test files to check (default: test files changed since the base)",
    )
    ap.add_argument(
        "--support",
        nargs="+",
        default=[],
        help="extra changed files the tests need on the base (fixtures, conftest)",
    )
    ap.add_argument("--test-cmd", default=DEFAULT_TEST_CMD)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--test-glob", action="append", default=None)
    ap.add_argument(
        "--assertion-type",
        action="append",
        default=[],
        help="extra exception class name that counts as an assertion failure",
    )
    ap.add_argument(
        "--assertion-pattern",
        action="append",
        default=[],
        help="regex on the failure message that counts as an assertion failure "
        "(for runners whose JUnit output names no assertion type)",
    )
    ap.add_argument(
        "--all-tests",
        action="store_true",
        help="judge every test in the changed files, not only new test ids",
    )
    ap.add_argument(
        "--require-tests",
        action="store_true",
        help="exit 1 when no new tests are found",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="treat NEW_SYMBOL (red only because the code is absent on the base) as bad",
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args(argv)

    workdir = Path(a.workdir).resolve()
    try:
        results, unreplayed = check(
            workdir,
            a.base,
            a.tests,
            a.support,
            a.test_cmd,
            a.timeout,
            tuple(a.test_glob or DEFAULT_TEST_GLOBS),
            tuple(a.assertion_type),
            a.all_tests,
            tuple(a.assertion_pattern),
        )
    except (RedCheckError, OSError) as exc:
        print(f"red-check BLOCK: {exc}", file=sys.stderr)
        return 2

    bad = [r for r in results if r["verdict"] in (STRICT_BAD if a.strict else BAD)]
    if a.json:
        print(
            json.dumps(
                {"base": a.base, "results": results, "not_replayed": unreplayed},
                indent=1,
            )
        )
    else:
        if not results:
            print("red-check: no new tests found")
        for r in results:
            line = f"{r['verdict']:<12} {r['test']}"
            if (
                r["verdict"] in ("OK", "WRONG_REASON", "NEW_SYMBOL")
                and r["base_message"]
            ):
                line += f"  (base: {r['base_message']})"
            print(line)
    if unreplayed:
        print(
            f"red-check: {len(unreplayed)} existing test(s) in the changed files were "
            "not replayed. If the branch changed an existing test's expectation, "
            "rerun with --all-tests and state why the old expectation was wrong.",
            file=sys.stderr,
        )
    new_symbol = [r for r in results if r["verdict"] == "NEW_SYMBOL"]
    if new_symbol and not a.strict:
        print(
            f"red-check: {len(new_symbol)} new test(s) exercise code the base does not "
            "have, so replaying them cannot show a right-reason red. Their evidence "
            "is the assertion red from test-first development (stub a wrong answer, "
            "watch the assertion fail); cite it in the PR.",
            file=sys.stderr,
        )
    if bad:
        print(
            f"red-check: {len(bad)} of {len(results)} new tests did not go red for "
            "the right reason on the base, or are not green on head.",
            file=sys.stderr,
        )
        return 1
    if not results and a.require_tests:
        print("red-check: no new tests, and --require-tests is set", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
