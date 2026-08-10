#!/usr/bin/env python3
"""Static-analysis pre-pass (D1) for the codex adversarial-review bridge.

Runs a trust-bounded, deterministic, host-safe analyzer set over the changed
files of a code review and returns a compact `## Deterministic tool findings`
digest for inlining into the adversarial generator prompt.

Trust model:
  * Default (always-on) analyzers are non-executing, no-network, no-repo-config
    parsers over file *contents*: `ruff --isolated` and `shellcheck --norc`.
  * `gitleaks` + `semgrep` are tool-present-gated OPTIONAL additions (absent on a
    machine → emit a `tool X unavailable` note, never fail).
  * Executing / config-loading analyzers (`eslint`, `tsc`, `mypy`,
    `semgrep --config auto`) run ONLY when the trust flag is set
    (`--trust-analyzers` / `CODEX_TRUST_ANALYZERS=1`), default off. When off the
    digest carries an `executing analyzers skipped ...` note so the omission is
    visible.

Graceful degradation: EVERY analyzer is gated on its binary being present
(`shutil.which`); an absent tool is skipped with a note, never a failure. The
optional gitleaks/semgrep configs ship vendored under `<plugin_root>/configs/`;
if a config is missing the analyzer is skipped with a note too.

Substrate split:
  * File-content analyzers (ruff/shellcheck/semgrep) run against a commit-era
    SCRATCH dir materialized via `git archive <rev> -- <paths> | tar -x` (for
    `--commit`, and for `--diff` when the worktree HEAD differs from the diff's
    HEAD side) so they analyze the REVIEWED bytes, not HEAD's.
  * History-aware `gitleaks git --log-opts` runs against the ORIGINAL `$WORKDIR`
    repo scoped to the rev/range (it needs real `.git` history the scratch lacks),
    NEVER the scratch. Each analyzer is tagged with its substrate so this cannot
    be mis-wired.

Review artifacts are LOCAL /tmp files shown only to the operator on their own
machine — there is no publish/egress surface, so the digest is not redacted by
default. A project that wants the digest scrubbed of the reviewed repo's own
secrets can pass a `redactor` callable to `run_prepass`.

Best-effort: a tool that is absent, errors, or times out (per-tool wall-clock
cap) emits a note and the pre-pass continues. A D1 failure is never a review
failure — this module never raises for an analyzer problem.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

DIGEST_HEADING = "## Deterministic tool findings"
DIGEST_FRAMING = (
    "These are deterministic findings on the changed files; treat as "
    "corroborating signal, not as your finding list."
)
DIGEST_LINE_CAP = 40
DEFAULT_TIMEOUT_S = 60

EXECUTING_SKIPPED_NOTE = (
    "executing analyzers skipped (untrusted-diff default; pass "
    "--trust-analyzers to enable)"
)
MATERIALIZE_FAILED_NOTE = (
    "file analyzers skipped (commit-era materialization failed; would otherwise "
    "analyze bytes outside the reviewed revision)"
)


def _plugin_root() -> Path:
    """Plugin root holding configs/. Env-first, then <root>/scripts/<this>."""
    env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[1]


# Pinned, vendored config locations. Both are tool-present-gated AND
# config-present-gated: a missing config skips the analyzer with a note.
PINNED_GITLEAKS_CONFIG = _plugin_root() / "configs" / "gitleaks.toml"
PINNED_SEMGREP_CONFIG = _plugin_root() / "configs" / "semgrep"

SUBSTRATE_FILE = "file"  # runs against the commit-era scratch
SUBSTRATE_HISTORY = "history"  # runs against the WORKDIR repo @ rev

TIER_DEFAULT = "default"  # always-on when present
TIER_OPTIONAL = "optional"  # tool-present-gated add-on
TIER_EXECUTING = "executing"  # opt-in behind the trust flag


@dataclass(frozen=True)
class Finding:
    tool: str
    rule: str
    location: str  # "file:line"
    message: str

    def line(self) -> str:
        parts = [self.tool]
        if self.rule:
            parts.append(self.rule)
        if self.location:
            parts.append(self.location)
        if self.message:
            parts.append(self.message)
        return " ".join(parts)


@dataclass(frozen=True)
class Analyzer:
    name: str
    tier: str
    substrate: str
    # File extensions the analyzer applies to; empty = applies whenever there is
    # any changed file (history-substrate tools scan a range, not paths).
    extensions: Tuple[str, ...]
    parser: Callable[[str, str], List[Finding]]


# ---------------------------------------------------------------------------
# Output parsers (defensive: fake/real analyzer output must never raise here).
# ---------------------------------------------------------------------------


def _parse_ruff(stdout: str, tool: str) -> List[Finding]:
    out: List[Finding] = []
    try:
        data = json.loads(stdout or "[]")
    except (ValueError, TypeError):
        return out
    if not isinstance(data, list):
        return out
    for item in data:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename") or item.get("file") or "")
        loc = item.get("location") or {}
        row = loc.get("row") if isinstance(loc, dict) else None
        location = f"{filename}:{row}" if row is not None else filename
        out.append(
            Finding(
                tool=tool,
                rule=str(item.get("code") or ""),
                location=location,
                message=str(item.get("message") or "").strip(),
            )
        )
    return out


def _parse_shellcheck(stdout: str, tool: str) -> List[Finding]:
    out: List[Finding] = []
    try:
        data = json.loads(stdout or "{}")
    except (ValueError, TypeError):
        return out
    comments = data.get("comments") if isinstance(data, dict) else data
    if not isinstance(comments, list):
        return out
    for item in comments:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("file") or "")
        line = item.get("line")
        location = f"{filename}:{line}" if line is not None else filename
        code = item.get("code")
        rule = f"SC{code}" if isinstance(code, int) else str(code or "")
        out.append(
            Finding(
                tool=tool,
                rule=rule,
                location=location,
                message=str(item.get("message") or "").strip(),
            )
        )
    return out


def _parse_eslint(stdout: str, tool: str) -> List[Finding]:
    out: List[Finding] = []
    try:
        data = json.loads(stdout or "[]")
    except (ValueError, TypeError):
        return out
    if not isinstance(data, list):
        return out
    for entry in data:
        if not isinstance(entry, dict):
            continue
        filename = str(entry.get("filePath") or "")
        for msg in entry.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            line = msg.get("line")
            location = f"{filename}:{line}" if line is not None else filename
            out.append(
                Finding(
                    tool=tool,
                    rule=str(msg.get("ruleId") or ""),
                    location=location,
                    message=str(msg.get("message") or "").strip(),
                )
            )
    return out


def _parse_gitleaks(stdout: str, tool: str) -> List[Finding]:
    out: List[Finding] = []
    try:
        data = json.loads(stdout or "[]")
    except (ValueError, TypeError):
        return out
    if not isinstance(data, list):
        return out
    for item in data:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("File") or item.get("file") or "")
        line = item.get("StartLine") or item.get("line")
        location = f"{filename}:{line}" if line is not None else filename
        out.append(
            Finding(
                tool=tool,
                rule=str(item.get("RuleID") or item.get("Description") or "secret"),
                location=location,
                message=str(item.get("Description") or "potential secret").strip(),
            )
        )
    return out


def _parse_semgrep(stdout: str, tool: str) -> List[Finding]:
    out: List[Finding] = []
    try:
        data = json.loads(stdout or "{}")
    except (ValueError, TypeError):
        return out
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return out
    for item in results:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("path") or "")
        start = item.get("start") or {}
        line = start.get("line") if isinstance(start, dict) else None
        location = f"{filename}:{line}" if line is not None else filename
        extra = item.get("extra") or {}
        message = extra.get("message") if isinstance(extra, dict) else ""
        out.append(
            Finding(
                tool=tool,
                rule=str(item.get("check_id") or ""),
                location=location,
                message=str(message or "").strip(),
            )
        )
    return out


def _parse_text_noop(stdout: str, tool: str) -> List[Finding]:
    """Best-effort parser for tools whose structured output we don't model.

    Emits one finding per non-empty stdout line so trust-flag runs surface
    signal without a bespoke parser; the reviewer treats it as corroborating.
    """
    out: List[Finding] = []
    for raw in (stdout or "").splitlines():
        text = raw.strip()
        if text:
            out.append(Finding(tool=tool, rule="", location="", message=text))
    return out


# ---------------------------------------------------------------------------
# Analyzer registry.
# ---------------------------------------------------------------------------

ANALYZERS: Tuple[Analyzer, ...] = (
    Analyzer("ruff", TIER_DEFAULT, SUBSTRATE_FILE, (".py",), _parse_ruff),
    Analyzer(
        "shellcheck", TIER_DEFAULT, SUBSTRATE_FILE, (".sh", ".bash"), _parse_shellcheck
    ),
    Analyzer(
        "semgrep",
        TIER_OPTIONAL,
        SUBSTRATE_FILE,
        (".py", ".js", ".jsx", ".ts", ".tsx"),
        _parse_semgrep,
    ),
    Analyzer("gitleaks", TIER_OPTIONAL, SUBSTRATE_HISTORY, (), _parse_gitleaks),
    Analyzer(
        "eslint",
        TIER_EXECUTING,
        SUBSTRATE_FILE,
        (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"),
        _parse_eslint,
    ),
    Analyzer("tsc", TIER_EXECUTING, SUBSTRATE_FILE, (".ts", ".tsx"), _parse_text_noop),
    Analyzer("mypy", TIER_EXECUTING, SUBSTRATE_FILE, (".py",), _parse_text_noop),
)


def analyzer_substrate_dir(
    analyzer: Analyzer, workdir: Path, scratch: Optional[Path]
) -> Path:
    """Resolve the directory an analyzer must run against for its substrate.

    file    → the commit-era scratch (falls back to workdir when not materialized)
    history → ALWAYS the original workdir repo (has the real `.git`); NEVER scratch.
    """
    if analyzer.substrate == SUBSTRATE_HISTORY:
        return workdir
    if scratch is not None:
        return scratch
    return workdir


def _invocation(
    analyzer: Analyzer,
    *,
    target_files: Sequence[str],
    base_dir: Path,
    history_log_opts: Optional[str],
) -> List[str]:
    """Build the exact argv for an analyzer (pinned, no-network invocations)."""
    if analyzer.name == "ruff":
        return [
            "ruff",
            "check",
            "--isolated",
            "--output-format=json",
            *[str(base_dir / f) for f in target_files],
        ]
    if analyzer.name == "shellcheck":
        # --norc: never read a branch-supplied .shellcheckrc. No -x/--external-sources.
        return [
            "shellcheck",
            "--norc",
            "--format=json1",
            *[str(base_dir / f) for f in target_files],
        ]
    if analyzer.name == "semgrep":
        return [
            "semgrep",
            "--config",
            str(PINNED_SEMGREP_CONFIG),
            "--metrics=off",
            "--disable-version-check",
            *[str(base_dir / f) for f in target_files],
        ]
    if analyzer.name == "gitleaks":
        # Modern non-deprecated invocation; NEVER `detect` / `protect --staged`.
        return [
            "gitleaks",
            "git",
            f"--log-opts={history_log_opts or ''}",
            "--config",
            str(PINNED_GITLEAKS_CONFIG),
            "--no-banner",
        ]
    if analyzer.name == "eslint":
        return [
            "eslint",
            "--format",
            "json",
            *[str(base_dir / f) for f in target_files],
        ]
    if analyzer.name == "tsc":
        return ["tsc", "--noEmit", *[str(base_dir / f) for f in target_files]]
    if analyzer.name == "mypy":
        return ["mypy", *[str(base_dir / f) for f in target_files]]
    raise ValueError(f"unknown analyzer {analyzer.name!r}")


def _config_present(analyzer: Analyzer) -> bool:
    """Optional analyzers require their vendored config to be present."""
    if analyzer.name == "gitleaks":
        return PINNED_GITLEAKS_CONFIG.exists()
    if analyzer.name == "semgrep":
        return PINNED_SEMGREP_CONFIG.exists()
    return True


def _applicable_files(analyzer: Analyzer, files: Sequence[str]) -> List[str]:
    if not analyzer.extensions:
        return list(files)
    return [f for f in files if Path(f).suffix in analyzer.extensions]


def _history_log_opts(mode: str, rev: str) -> Optional[str]:
    if mode == "commit":
        return f"-1 {rev}"
    if mode == "diff":
        return rev  # caller passes the range (e.g. "base..HEAD")
    return None


def _paths_dirty(workdir: Path, paths: Sequence[str]) -> bool:
    """True if any of `paths` has uncommitted changes in the worktree.

    Fail-safe: if the status probe cannot run, return True so the caller
    materializes the committed bytes rather than risk analyzing dirty ones.
    """
    if not paths:
        return False
    try:
        out = _git(workdir, ["status", "--porcelain", "--", *paths])
    except Exception:
        return True
    return bool(out.strip())


def _should_materialize(
    mode: str, workdir: Path, rev: str, paths: Sequence[str] = ()
) -> Tuple[bool, Optional[str]]:
    """Decide whether to materialize commit-era files and at which ref.

    commit → always (reviewed bytes live at the sha, not HEAD).
    diff   → when the worktree HEAD differs from the diff's HEAD side, OR when
             the reviewed paths are dirty in the worktree. The dirty case is
             load-bearing: with HEAD == worktree HEAD the analyzers would run
             in-place and see UNCOMMITTED bytes, not the reviewed HEAD bytes.
             Materializing the HEAD side pins the committed bytes.
    files  → never (files are analyzed in place).
    """
    if mode == "commit":
        return True, rev
    if mode == "diff":
        head_side = rev.split("..")[-1].strip() if ".." in rev else "HEAD"
        try:
            head_sha = _git(workdir, ["rev-parse", head_side])
            wt_sha = _git(workdir, ["rev-parse", "HEAD"])
        except Exception:
            return False, None
        if head_sha and wt_sha and head_sha != wt_sha:
            return True, head_side
        # HEAD side == worktree HEAD: in-place bytes equal the reviewed bytes
        # ONLY when the reviewed paths are clean. A dirty worktree would feed
        # uncommitted bytes to the analyzers, so pin the HEAD side.
        if _paths_dirty(workdir, paths):
            return True, head_side
        return False, None
    return False, None


def _git(workdir: Path, args: Sequence[str]) -> str:
    proc = subprocess.run(
        ["git", "-C", str(workdir), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _materialize(
    workdir: Path, ref: str, paths: Sequence[str], scratch: Path
) -> Optional[Path]:
    """Materialize commit-era file contents into `scratch` via git archive|tar.

    Returns the scratch dir on success, None on failure (caller falls back to
    analyzing workdir paths — never raises).
    """
    if not paths:
        return None
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        archive = subprocess.run(
            ["git", "-C", str(workdir), "archive", ref, "--", *paths],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["tar", "-x", "-C", str(scratch)],
            input=archive.stdout,
            capture_output=True,
            check=True,
        )
        return scratch
    except Exception:
        return None


def _run_analyzer(
    analyzer: Analyzer,
    *,
    target_files: Sequence[str],
    base_dir: Path,
    history_log_opts: Optional[str],
    timeout_s: int,
) -> Tuple[List[Finding], List[str]]:
    """Run one analyzer. Returns (findings, notes). Never raises."""
    findings: List[Finding] = []
    notes: List[str] = []
    if shutil.which(analyzer.name) is None:
        notes.append(f"tool {analyzer.name} unavailable")
        return findings, notes
    if not _config_present(analyzer):
        notes.append(f"tool {analyzer.name} skipped (vendored config missing)")
        return findings, notes
    cwd = base_dir if analyzer.substrate == SUBSTRATE_HISTORY else None
    argv = _invocation(
        analyzer,
        target_files=target_files if analyzer.substrate == SUBSTRATE_FILE else [],
        base_dir=base_dir if analyzer.substrate == SUBSTRATE_FILE else Path("."),
        history_log_opts=history_log_opts,
    )
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        notes.append(f"tool {analyzer.name} timed out")
        return findings, notes
    except Exception:
        notes.append(f"tool {analyzer.name} errored")
        return findings, notes
    try:
        findings = analyzer.parser(proc.stdout, analyzer.name)
    except Exception:
        notes.append(f"tool {analyzer.name} output unparseable")
    return findings, notes


def run_prepass(
    *,
    mode: str,
    workdir: Path,
    changed_files: Sequence[Tuple[str, str]],
    rev: str,
    trust: bool = False,
    redactor: Optional[Callable[[str], str]] = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    scratch_root: Optional[Path] = None,
) -> str:
    """Run the D1 pre-pass and return the digest text.

    changed_files: sequence of (status, path). Deleted paths (status starting
    with 'D') are skipped for file analyzers (no blob at the ref).

    redactor: optional callable applied to the assembled digest. None == no
    redaction (review artifacts are local-only; there is no egress surface).
    """
    workdir = Path(workdir)
    non_deleted = [
        p for (status, p) in changed_files if not str(status).upper().startswith("D")
    ]

    findings: List[Finding] = []
    notes: List[str] = []

    # --- Substrate materialization (commit-era bytes for file analyzers) ---
    scratch: Optional[Path] = None
    scratch_dir: Optional[Path] = (
        None  # the ALLOCATED dir (cleaned regardless of outcome)
    )
    owns_scratch = False
    materialize_failed = False
    do_materialize, ref = _should_materialize(mode, workdir, rev, non_deleted)
    if do_materialize and non_deleted:
        if scratch_root is not None:
            scratch_dir = Path(scratch_root)
        else:
            scratch_dir = Path(tempfile.mkdtemp(prefix="review-prepass-scratch-"))
            owns_scratch = True
        materialized = _materialize(workdir, ref or rev, non_deleted, scratch_dir)
        scratch = materialized
        # Materialization was REQUIRED (commit-era bytes, or a dirty diff worktree)
        # but failed. Falling back to the in-place workdir would feed the analyzers
        # bytes OUTSIDE the reviewed revision — the exact substrate this pins away
        # from. Skip the file analyzers and emit a visible note instead; never
        # silently analyze the wrong bytes.
        materialize_failed = materialized is None

    try:
        history_log_opts = _history_log_opts(mode, rev)
        code_review = mode in {"diff", "commit", "files"}

        for analyzer in ANALYZERS:
            if analyzer.tier == TIER_EXECUTING and not trust:
                continue  # gated; the skipped note is added once below
            if analyzer.substrate == SUBSTRATE_FILE:
                if materialize_failed:
                    continue  # skipped; the note is added once below
                target_files = _applicable_files(analyzer, non_deleted)
                if not target_files:
                    continue  # not applicable to this changeset
                base_dir = analyzer_substrate_dir(analyzer, workdir, scratch)
            else:  # history
                if mode not in {"diff", "commit"} or not non_deleted:
                    continue  # no history scope for --files
                base_dir = analyzer_substrate_dir(analyzer, workdir, scratch)
                target_files = []
            tool_findings, tool_notes = _run_analyzer(
                analyzer,
                target_files=target_files,
                base_dir=base_dir,
                history_log_opts=history_log_opts,
                timeout_s=timeout_s,
            )
            findings.extend(tool_findings)
            notes.extend(tool_notes)

        if materialize_failed:
            notes.append(MATERIALIZE_FAILED_NOTE)
        if code_review and not trust:
            notes.append(EXECUTING_SKIPPED_NOTE)
    finally:
        # Clean the ALLOCATED scratch dir whenever we own it — even if
        # materialization failed (scratch is None but scratch_dir was mkdtemp'd),
        # or partially populated before erroring. Keying cleanup on `scratch`
        # (the success result) would leak the dir on every failed materialization
        # and could exhaust /tmp under a persistent git/tar fault.
        if owns_scratch and scratch_root is None and scratch_dir is not None:
            shutil.rmtree(scratch_dir, ignore_errors=True)

    return _assemble_digest(findings, notes, redactor)


def _assemble_digest(
    findings: Sequence[Finding],
    notes: Sequence[str],
    redactor: Optional[Callable[[str], str]],
) -> str:
    # Dedupe finding lines, preserving first-seen order.
    seen = set()
    lines: List[str] = []
    for f in findings:
        line = f.line()
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)

    overflow = 0
    if len(lines) > DIGEST_LINE_CAP:
        overflow = len(lines) - DIGEST_LINE_CAP
        lines = lines[:DIGEST_LINE_CAP]

    parts: List[str] = [DIGEST_HEADING, "", DIGEST_FRAMING, ""]
    if lines:
        parts.extend(lines)
        if overflow:
            parts.append(f"+{overflow} more")
    else:
        parts.append("(no deterministic findings on the changed files)")
    if notes:
        parts.append("")
        parts.append("Notes:")
        parts.extend(f"- {n}" for n in notes)
    body = "\n".join(parts) + "\n"

    if redactor is None:
        return body
    try:
        redacted = redactor(body)
    except Exception:
        return body
    return redacted if redacted is not None else body


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_changed(values: Sequence[str]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for raw in values:
        if "\t" in raw:
            # git --name-status is TAB-delimited. A rename/copy emits three
            # fields — `R<score>\t<src>\t<dst>` (likewise `C<score>...`) — and the
            # TARGET path is the LAST field. A plain change is `<status>\t<path>`.
            # Splitting on the FIRST tab only left the target as `<src>\t<dst>`,
            # so the renamed file silently dropped from the analyzer set (its blob
            # was unreadable at the ref).
            fields = raw.split("\t")
            status, path = fields[0], fields[-1]
        else:
            status, _, path = raw.partition(":")
        out.append((status.strip(), path.strip()))
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Static-analysis pre-pass (D1)")
    parser.add_argument("--mode", required=True, choices=["diff", "commit", "files"])
    parser.add_argument("--workdir", required=True)
    parser.add_argument(
        "--rev", required=True, help="reviewed rev (sha) or range (base..HEAD)"
    )
    parser.add_argument(
        "--changed",
        action="append",
        default=[],
        help="repeatable STATUS:PATH (or STATUS<TAB>PATH) for each changed file",
    )
    parser.add_argument("--trust-analyzers", action="store_true")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    args = parser.parse_args(argv)

    trust = args.trust_analyzers or os.environ.get("CODEX_TRUST_ANALYZERS") == "1"

    digest = run_prepass(
        mode=args.mode,
        workdir=Path(args.workdir),
        changed_files=_parse_changed(args.changed),
        rev=args.rev,
        trust=trust,
        timeout_s=args.timeout,
    )
    sys.stdout.write(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
