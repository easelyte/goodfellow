"""Deterministic contract tests for review_prepass.py.

Fake analyzers are placed on PATH via a tmp bin dir. No real Codex or network
calls.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from review_prepass import (
    ANALYZERS,
    EXECUTING_SKIPPED_NOTE,
    _history_log_opts,
    _parse_changed,
    _should_materialize,
    analyzer_substrate_dir,
    run_prepass,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _fake(bin_dir: Path, name: str, stdout: str = "") -> Path:
    """Write an executable fake analyzer that logs argv+pwd and prints stdout."""
    path = bin_dir / name
    script = (
        "#!/usr/bin/env bash\n"
        'self=$(basename "$0")\n'
        'if [[ -n "${SENTINEL_DIR:-}" ]]; then\n'
        '  { echo "ARGV: $*"; echo "PWD: $(pwd)"; } >> "${SENTINEL_DIR}/${self}.log"\n'
        "fi\n"
        f"printf '%s' {_shq(stdout)}\n"
    )
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _shq(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tester@example.com")
    _git(root, "config", "user.name", "tester")


@pytest.fixture
def bins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sentinel_dir = tmp_path / "sentinel"
    sentinel_dir.mkdir()
    monkeypatch.setenv(
        "PATH", f"{bin_dir}:{Path('/usr/bin')}:{Path('/bin')}:{Path('/usr/local/bin')}"
    )
    monkeypatch.setenv("SENTINEL_DIR", str(sentinel_dir))
    return bin_dir, sentinel_dir


# ---------------------------------------------------------------------------
# Trust boundary / gating
# ---------------------------------------------------------------------------


def test_trust_gating(bins, tmp_path: Path) -> None:
    bin_dir, sentinel = bins
    for name, out in (
        ("ruff", "[]"),
        ("shellcheck", '{"comments":[]}'),
        ("gitleaks", "[]"),
        ("eslint", "[]"),
        ("tsc", ""),
        ("mypy", ""),
    ):
        _fake(bin_dir, name, out)

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "b.sh").write_text("echo hi\n", encoding="utf-8")
    (repo / "c.ts").write_text("export const x = 1\n", encoding="utf-8")
    _git(repo, "add", "a.py", "b.sh", "c.ts")
    _git(repo, "commit", "-q", "-m", "add")
    sha = _git(repo, "rev-parse", "HEAD")
    changed = [("A", "a.py"), ("A", "b.sh"), ("A", "c.ts")]

    # trust OFF: executing analyzers must NOT run; default/optional-present DO.
    digest = run_prepass(
        mode="commit",
        workdir=repo,
        changed_files=changed,
        rev=sha,
        trust=False,
    )
    assert (sentinel / "ruff.log").exists()
    assert (sentinel / "shellcheck.log").exists()
    assert (sentinel / "gitleaks.log").exists()
    assert not (sentinel / "eslint.log").exists()
    assert not (sentinel / "tsc.log").exists()
    assert not (sentinel / "mypy.log").exists()
    assert EXECUTING_SKIPPED_NOTE in digest

    # trust ON: eslint (executing, applies to .ts) now runs.
    for f in sentinel.iterdir():
        f.unlink()
    run_prepass(
        mode="commit",
        workdir=repo,
        changed_files=changed,
        rev=sha,
        trust=True,
    )
    assert (sentinel / "eslint.log").exists()


# ---------------------------------------------------------------------------
# No-network / no-repo-config invocation strings + optional-absent notes
# ---------------------------------------------------------------------------


def test_invocation_strings_and_absent_notes(bins, tmp_path: Path) -> None:
    bin_dir, sentinel = bins
    # Only ruff + shellcheck present; gitleaks + semgrep intentionally absent.
    _fake(bin_dir, "ruff", "[]")
    _fake(bin_dir, "shellcheck", '{"comments":[]}')

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "b.sh").write_text("echo hi\n", encoding="utf-8")
    _git(repo, "add", "a.py", "b.sh")
    _git(repo, "commit", "-q", "-m", "add")
    sha = _git(repo, "rev-parse", "HEAD")

    digest = run_prepass(
        mode="commit",
        workdir=repo,
        changed_files=[("A", "a.py"), ("A", "b.sh")],
        rev=sha,
        trust=False,
    )

    ruff_log = (sentinel / "ruff.log").read_text(encoding="utf-8")
    shellcheck_log = (sentinel / "shellcheck.log").read_text(encoding="utf-8")
    assert "--isolated" in ruff_log
    assert "--norc" in shellcheck_log
    assert " -x" not in shellcheck_log
    assert "--external-sources" not in shellcheck_log

    # gitleaks + semgrep applicable but absent → unavailable notes; review completes.
    assert "tool gitleaks unavailable" in digest
    assert "tool semgrep unavailable" in digest


# ---------------------------------------------------------------------------
# Commit-era materialization + substrate split
# ---------------------------------------------------------------------------


def test_commit_era_scratch_not_head(bins, tmp_path: Path) -> None:
    bin_dir, sentinel = bins
    _fake(bin_dir, "ruff", "[]")

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "lint.py").write_text("V1_commit_era = 1\n", encoding="utf-8")
    _git(repo, "add", "lint.py")
    _git(repo, "commit", "-q", "-m", "c1")
    sha1 = _git(repo, "rev-parse", "HEAD")
    # Change the file AFTER the reviewed sha.
    (repo / "lint.py").write_text("V2_head_bytes = 2\n", encoding="utf-8")
    _git(repo, "add", "lint.py")
    _git(repo, "commit", "-q", "-m", "c2")

    scratch = tmp_path / "scratch"
    run_prepass(
        mode="commit",
        workdir=repo,
        changed_files=[("A", "lint.py")],
        rev=sha1,
        trust=False,
        scratch_root=scratch,
    )

    # ruff was pointed at the materialized scratch copy, not the HEAD file.
    ruff_log = (sentinel / "ruff.log").read_text(encoding="utf-8")
    assert str(scratch) in ruff_log
    materialized = (scratch / "lint.py").read_text(encoding="utf-8")
    assert "V1_commit_era" in materialized  # commit-era bytes
    assert "V2_head_bytes" not in materialized  # NOT HEAD


def test_history_substrate_never_scratch() -> None:
    by_name = {a.name: a for a in ANALYZERS}
    workdir = Path("/repo")
    scratch = Path("/tmp/scratch")
    # history-aware gitleaks always resolves to the workdir repo, never scratch.
    assert analyzer_substrate_dir(by_name["gitleaks"], workdir, scratch) == workdir
    # file-content ruff resolves to the commit-era scratch.
    assert analyzer_substrate_dir(by_name["ruff"], workdir, scratch) == scratch


# ---------------------------------------------------------------------------
# Redaction: local-only by default; caller may supply a redactor
# ---------------------------------------------------------------------------


def test_no_redactor_by_default_finding_present(bins, tmp_path: Path) -> None:
    bin_dir, _ = bins
    _fake(
        bin_dir,
        "ruff",
        '[{"code":"E501","filename":"m.py","location":{"row":3},'
        '"message":"line too long"}]',
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("x = 1\n", encoding="utf-8")

    digest = run_prepass(
        mode="files",
        workdir=repo,
        changed_files=[("A", "m.py")],
        rev="HEAD",
        trust=False,
    )
    assert "E501" in digest  # finding present, no redaction by default


def test_supplied_redactor_is_applied(bins, tmp_path: Path) -> None:
    bin_dir, _ = bins
    _fake(
        bin_dir,
        "ruff",
        '[{"code":"E501","filename":"secret.py","location":{"row":3},'
        '"message":"leaked sk-SECRET123 here"}]',
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "secret.py").write_text("x = 1\n", encoding="utf-8")

    def redactor(text: str) -> str:
        return text.replace("sk-SECRET123", "[REDACTED]")

    digest = run_prepass(
        mode="files",
        workdir=repo,
        changed_files=[("A", "secret.py")],
        rev="HEAD",
        trust=False,
        redactor=redactor,
    )
    assert "sk-SECRET123" not in digest
    assert "[REDACTED]" in digest
    assert "E501" in digest  # finding still present, just redacted


# ---------------------------------------------------------------------------
# Rename parsing / history scope / dirty-worktree materialization
# ---------------------------------------------------------------------------


def test_parse_changed_rename_uses_target_path() -> None:
    """Rename status `R100\\tsrc\\tdst` must parse to the TARGET path (last field)
    so the renamed file is analyzed, not the unreadable `src\\tdst` blob."""
    assert _parse_changed(["R096\told.py\tnew.py"]) == [("R096", "new.py")]
    assert _parse_changed(["M\tkeep.py"]) == [("M", "keep.py")]


def test_history_log_opts_diff_is_range_not_head() -> None:
    """gitleaks history scope: diff mode threads the RANGE (base..HEAD), not a
    bare ref, so it scans the PR's own commits — not all of HEAD's history."""
    assert _history_log_opts("diff", "main..HEAD") == "main..HEAD"
    assert _history_log_opts("commit", "abc123") == "-1 abc123"
    assert _history_log_opts("files", "HEAD") is None


def test_materialize_on_dirty_worktree(bins, tmp_path: Path) -> None:
    """--diff with HEAD == worktree HEAD but a DIRTY changed path must materialize
    the committed HEAD bytes; analyzers must not see the uncommitted edit."""
    bin_dir, sentinel = bins
    _fake(bin_dir, "ruff", "[]")

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "d.py").write_text("committed = 1\n", encoding="utf-8")
    _git(repo, "add", "d.py")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "e.py").write_text("committed_e = 1\n", encoding="utf-8")
    _git(repo, "add", "e.py")
    _git(repo, "commit", "-q", "-m", "second")
    # Dirty the reviewed path AFTER committing — HEAD == worktree HEAD.
    (repo / "e.py").write_text("uncommitted = 999\n", encoding="utf-8")

    # Decision-level: dirty changed path forces materialization at HEAD.
    do_mat, ref = _should_materialize("diff", repo, f"{base}..HEAD", ["e.py"])
    assert do_mat is True
    assert ref == "HEAD"

    scratch = tmp_path / "scratch"
    run_prepass(
        mode="diff",
        workdir=repo,
        changed_files=[("M", "e.py")],
        rev=f"{base}..HEAD",
        trust=False,
        scratch_root=scratch,
    )
    ruff_log = (sentinel / "ruff.log").read_text(encoding="utf-8")
    assert str(scratch) in ruff_log  # analyzed the materialized copy
    materialized = (scratch / "e.py").read_text(encoding="utf-8")
    assert "committed_e" in materialized  # committed HEAD bytes
    assert "uncommitted" not in materialized  # NOT the dirty worktree bytes


def test_clean_worktree_analyzes_in_place(tmp_path: Path) -> None:
    """A clean changed path needs no materialization — in-place bytes == HEAD."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "c.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "c.py")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "c.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "add", "c.py")
    _git(repo, "commit", "-q", "-m", "second")
    base = _git(repo, "rev-parse", "HEAD~1")

    do_mat, ref = _should_materialize("diff", repo, f"{base}..HEAD", ["c.py"])
    assert do_mat is False
    assert ref is None


def test_required_materialization_failure_skips_file_analyzers(
    bins, tmp_path: Path, monkeypatch
) -> None:
    """When materialization is REQUIRED (dirty diff path) but fails, file
    analyzers must be SKIPPED with a visible note — never fall back to the dirty
    worktree bytes the materialization exists to exclude."""
    import review_prepass as rp

    bin_dir, sentinel = bins
    _fake(
        bin_dir,
        "ruff",
        '[{"code":"E999","filename":"x.py","location":{"row":1},"message":"dirty"}]',
    )

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "x.py").write_text("committed = 1\n", encoding="utf-8")
    _git(repo, "add", "x.py")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "x.py").write_text("committed_2 = 1\n", encoding="utf-8")
    _git(repo, "add", "x.py")
    _git(repo, "commit", "-q", "-m", "second")
    base = _git(repo, "rev-parse", "HEAD~1")
    (repo / "x.py").write_text("uncommitted = 1\n", encoding="utf-8")

    monkeypatch.setattr(rp, "_materialize", lambda *a, **k: None)
    # Pin mkdtemp under tmp_path so we can assert the failed-materialization
    # scratch dir is still cleaned up (no /tmp leak).
    scratch_home = tmp_path / "scratch_home"
    scratch_home.mkdir()
    monkeypatch.setattr(rp.tempfile, "tempdir", str(scratch_home))

    digest = run_prepass(
        mode="diff",
        workdir=repo,
        changed_files=[("M", "x.py")],
        rev=f"{base}..HEAD",
        trust=False,
    )
    assert rp.MATERIALIZE_FAILED_NOTE in digest
    assert "E999" not in digest
    assert not (sentinel / "ruff.log").exists()
    # The owned scratch dir must be removed even though materialization failed.
    assert list(scratch_home.iterdir()) == []
