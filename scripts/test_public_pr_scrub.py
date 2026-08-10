"""Tests for the public-PR internal-ref scrub gate."""

from __future__ import annotations

import subprocess
from pathlib import Path

import public_pr_scrub as gate


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "tester@example.com")
    _git(repo, "config", "user.name", "tester")
    (repo / "a.txt").write_text("baseline\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _feature_commit(repo: Path, filename: str, content: str) -> str:
    (repo / filename).write_text(content)
    _git(repo, "add", filename)
    _git(repo, "commit", "-q", "-m", f"add {filename}")
    return _git(repo, "rev-parse", "HEAD~1")


def _denylist(tmp_path: Path, *phrases: str) -> Path:
    p = tmp_path / "deny.txt"
    p.write_text("# internal tokens\n" + "\n".join(phrases) + "\n")
    return p


def test_hit_in_added_line_blocks(tmp_path):
    repo = _repo(tmp_path)
    base = _feature_commit(repo, "b.txt", "leak: ACME-INTERNAL host\n")
    dl = _denylist(tmp_path, "ACME-INTERNAL")
    rc = gate.main(["--workdir", str(repo), "--base", base, "--denylist", str(dl)])
    assert rc == 1


def test_clean_diff_passes(tmp_path):
    repo = _repo(tmp_path)
    base = _feature_commit(repo, "b.txt", "a perfectly ordinary line\n")
    dl = _denylist(tmp_path, "ACME-INTERNAL", "secret-host-42")
    rc = gate.main(["--workdir", str(repo), "--base", base, "--denylist", str(dl)])
    assert rc == 0


def test_word_boundary_no_false_positive(tmp_path):
    # 'CAT' must not trip on 'category' (word-boundary match, shared with CI scan).
    repo = _repo(tmp_path)
    base = _feature_commit(repo, "b.txt", "this is a category of things\n")
    dl = _denylist(tmp_path, "CAT")
    rc = gate.main(["--workdir", str(repo), "--base", base, "--denylist", str(dl)])
    assert rc == 0


def test_no_denylist_skips_by_default(tmp_path):
    repo = _repo(tmp_path)
    base = _feature_commit(repo, "b.txt", "anything\n")
    rc = gate.main(["--workdir", str(repo), "--base", base])
    assert rc == 0  # no denylist → pass with a note


def test_no_denylist_with_require_flag_fails(tmp_path):
    repo = _repo(tmp_path)
    base = _feature_commit(repo, "b.txt", "anything\n")
    rc = gate.main(["--workdir", str(repo), "--base", base, "--require-denylist"])
    assert rc == 2


def test_env_var_denylist_resolution(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    base = _feature_commit(repo, "b.txt", "contains SECRET_TOKEN_X here\n")
    dl = _denylist(tmp_path, "SECRET_TOKEN_X")
    monkeypatch.setenv("GOODFELLOW_INTERNAL_DENYLIST", str(dl))
    rc = gate.main(["--workdir", str(repo), "--base", base])
    assert rc == 1


def test_project_dot_goodfellow_denylist_resolution(tmp_path):
    repo = _repo(tmp_path)
    base = _feature_commit(repo, "b.txt", "mentions INTERNAL_NAME somewhere\n")
    gf = repo / ".goodfellow"
    gf.mkdir()
    (gf / "internal_denylist.txt").write_text("INTERNAL_NAME\n")
    rc = gate.main(["--workdir", str(repo), "--base", base])
    assert rc == 1


def test_resolve_precedence_explicit_over_env(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit.txt"
    explicit.write_text("X\n")
    monkeypatch.setenv("GOODFELLOW_INTERNAL_DENYLIST", str(tmp_path / "env.txt"))
    resolved = gate.resolve_denylist_path(str(explicit), tmp_path)
    assert resolved == explicit


# --- Fail-closed on an uncomputable diff (B1) -------------------------------
# A security gate must NEVER report clean when it scanned nothing. An invalid /
# unknown base makes `git diff <base>...HEAD` fail; the gate must exit 2
# (fail-closed), not scan empty stdout and exit 0.
def test_invalid_base_fails_closed(tmp_path):
    repo = _repo(tmp_path)
    _feature_commit(repo, "b.txt", "leak: ACME-INTERNAL host\n")
    dl = _denylist(tmp_path, "ACME-INTERNAL")
    rc = gate.main(
        ["--workdir", str(repo), "--base", "DOES_NOT_EXIST_REF", "--denylist", str(dl)]
    )
    assert rc == 2  # NOT 0 — the diff could not be built, so nothing was scanned


def test_added_lines_raises_on_bad_base(tmp_path):
    repo = _repo(tmp_path)
    _feature_commit(repo, "b.txt", "anything\n")
    import pytest

    with pytest.raises(gate.ScrubError):
        gate.added_lines(repo, "NO_SUCH_REF")


def test_non_repo_workdir_fails_closed(tmp_path):
    # A non-repository workdir → git diff errors → fail-closed, never clean.
    plain = tmp_path / "not_a_repo"
    plain.mkdir()
    dl = _denylist(tmp_path, "ACME-INTERNAL")
    rc = gate.main(["--workdir", str(plain), "--base", "main", "--denylist", str(dl)])
    assert rc == 2
