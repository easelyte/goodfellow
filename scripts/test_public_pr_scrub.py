"""Tests for the public-PR internal-ref scrub gate."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

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
    with pytest.raises(gate.ScrubError):
        gate.added_lines(repo, "NO_SUCH_REF")


def test_non_repo_workdir_fails_closed(tmp_path):
    # A non-repository workdir → git diff errors → fail-closed, never clean.
    plain = tmp_path / "not_a_repo"
    plain.mkdir()
    dl = _denylist(tmp_path, "ACME-INTERNAL")
    rc = gate.main(["--workdir", str(plain), "--base", "main", "--denylist", str(dl)])
    assert rc == 2


# --- Fail-open gaps found by mutation testing --------------------------------
# Each test names the break it kills. Tests that drive `default_bases` use a fake
# git, so a mutant of that code never touches a real repository.

class _FakeGit:
    """Stands in for `public_pr_scrub._git`: answers `rev-parse --verify` from a
    set of existing refs and `merge-base <ref> HEAD` from the refs that already
    contain HEAD, and fails the test on any other git call."""

    def __init__(
        self,
        refs=(),
        contain_head=(),
        head="h" * 40,
        head_rc=None,
        no_merge_base=(),
        no_merge_base_rc=1,
    ):
        self.no_merge_base = set(no_merge_base)
        self.no_merge_base_rc = no_merge_base_rc
        self.refs = set(refs)
        self.contain_head = set(contain_head)
        self.head = head
        self.head_rc = head_rc
        self.calls = []

    def __call__(self, workdir, args):
        self.calls.append(list(args))
        ok = subprocess.CompletedProcess
        if args[:1] == ["rev-parse"]:
            if args[-1] == "HEAD":
                rc = self.head_rc if self.head_rc is not None else (0 if self.head else 1)
                return ok(args, rc, (self.head or "") + "\n", "")
            return ok(args, 0 if args[-1] in self.refs else 1, "", "")
        if args[:1] == ["merge-base"] and args[2:] == ["HEAD"]:
            if args[1] in self.no_merge_base:
                return ok(args, self.no_merge_base_rc, "", "")
            sha = self.head if args[1] in self.contain_head else "b" * 40
            return ok(args, 0, sha + "\n", "")
        raise AssertionError(f"unexpected git call {args}")


@pytest.mark.parametrize(
    "refs,expected",
    [
        (
            set(gate.DEFAULT_BASE_REFS),
            list(gate.DEFAULT_BASE_REFS),  # all of them, in the documented order
        ),
        ({"origin/main", "upstream/main"}, ["upstream/main", "origin/main"]),
        ({"master", "origin/HEAD"}, ["origin/HEAD", "master"]),
        ({"main"}, ["main"]),
    ],
)
def test_default_bases_are_every_existing_candidate(monkeypatch, refs, expected):
    # Breaks killed: the `returncode != 0` test negated or swapped, the append
    # dropped. The branch's upstream (`@{upstream}`) is never consulted.
    fake = _FakeGit(refs)
    monkeypatch.setattr(gate, "_git", fake)
    assert gate.default_bases(Path("/nowhere")) == expected
    assert not any("@{upstream}" in arg for call in fake.calls for arg in call)


def test_default_bases_skip_a_ref_that_contains_head(monkeypatch):
    # Break killed: `!= head_sha` -> `== head_sha` (or the check dropped).
    fake = _FakeGit({"origin/HEAD", "origin/main", "main"}, contain_head={"origin/HEAD"})
    monkeypatch.setattr(gate, "_git", fake)
    assert gate.default_bases(Path("/nowhere")) == ["origin/main", "main"]


def test_default_bases_all_candidates_contain_head_fails_closed(monkeypatch):
    fake = _FakeGit({"origin/HEAD", "main"}, contain_head={"origin/HEAD", "main"})
    monkeypatch.setattr(gate, "_git", fake)
    with pytest.raises(gate.ScrubError):
        gate.default_bases(Path("/nowhere"))


@pytest.mark.parametrize(
    "head,head_rc", [("", 1), ("", 0), ("fatal: output on a failed call", 128)]
)
def test_default_bases_unresolvable_head_fails_closed(monkeypatch, head, head_rc):
    # Either signal alone (a nonzero exit, or no sha) means HEAD is unknown.
    # Break killed: `or` -> `and`.
    monkeypatch.setattr(gate, "_git", _FakeGit({"main"}, head=head, head_rc=head_rc))
    with pytest.raises(gate.ScrubError):
        gate.default_bases(Path("/nowhere"))


def test_default_bases_with_nothing_to_compare_fails_closed(monkeypatch):
    # The old fallback was HEAD~1: scan the LAST COMMIT ONLY, so a leak in any
    # earlier commit of the branch passed as clean.
    monkeypatch.setattr(gate, "_git", _FakeGit(set()))
    with pytest.raises(gate.ScrubError):
        gate.default_bases(Path("/nowhere"))


def test_multi_commit_leak_without_known_base_is_not_reported_clean(tmp_path):
    # End to end through the CLI: a branch with no upstream and no main/master.
    repo = _repo(tmp_path)
    _git(repo, "branch", "-m", "trunk")
    _feature_commit(repo, "b.txt", "leak: ACME-INTERNAL host\n")
    _feature_commit(repo, "c.txt", "an ordinary later commit\n")
    dl = _denylist(tmp_path, "ACME-INTERNAL")
    rc = gate.main(["--workdir", str(repo), "--denylist", str(dl)])
    assert rc == 2


def test_multi_commit_leak_found_with_default_base(tmp_path):
    # With a resolvable base, the whole branch is scanned, not just its tip.
    repo = _repo(tmp_path)
    _git(repo, "branch", "-m", "main")
    _git(repo, "checkout", "-q", "-b", "feature")
    _feature_commit(repo, "b.txt", "leak: ACME-INTERNAL host\n")
    _feature_commit(repo, "c.txt", "an ordinary later commit\n")
    dl = _denylist(tmp_path, "ACME-INTERNAL")
    assert gate.main(["--workdir", str(repo), "--denylist", str(dl)]) == 1


def _run_cli_without_git(tmp_path, *extra):
    empty_bin = tmp_path / "empty_bin"
    empty_bin.mkdir()
    dl = _denylist(tmp_path, "ACME-INTERNAL")
    env = dict(os.environ, PATH=str(empty_bin))
    env.pop("GOODFELLOW_INTERNAL_DENYLIST", None)
    return subprocess.run(
        [
            sys.executable,
            str(Path(gate.__file__)),
            "--workdir",
            str(tmp_path),
            "--denylist",
            str(dl),
            *extra,
        ],
        capture_output=True,
        text=True,
        env=env,
    )


def test_git_missing_with_explicit_base_fails_closed(tmp_path):
    # Break killed: the OSError branch's `return 2` -> None (exit 0).
    proc = _run_cli_without_git(tmp_path, "--base", "main")
    assert proc.returncode == 2
    assert "could not run git" in proc.stderr


def test_git_missing_with_default_base_fails_closed(tmp_path):
    # Resolving the default base runs git too; that failure must also be exit 2,
    # not a traceback (exit 1 means "hits found", which is a different verdict).
    proc = _run_cli_without_git(tmp_path)
    assert proc.returncode == 2
    assert "could not run git" in proc.stderr


def test_removed_and_context_lines_are_not_scanned(tmp_path):
    # Only ADDED lines ship. Break killed: `and not` -> `or not` in the filter,
    # which scans removed and unchanged lines too.
    repo = _repo(tmp_path)
    (repo / "a.txt").write_text("ACME-INTERNAL old line\nkeep CONTEXT-TOKEN\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "old")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("keep CONTEXT-TOKEN\nnew clean line\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "scrub")
    dl = _denylist(tmp_path, "ACME-INTERNAL", "CONTEXT-TOKEN")
    rc = gate.main(["--workdir", str(repo), "--base", base, "--denylist", str(dl)])
    assert rc == 0


def test_hit_at_start_of_added_line_blocks(tmp_path):
    # Only the diff's own '+' marker is stripped. Break killed: `line[1:]` ->
    # `line[2:]`, which eats the first character of the phrase.
    repo = _repo(tmp_path)
    base = _feature_commit(repo, "b.txt", "ACME-INTERNAL at column zero\n")
    dl = _denylist(tmp_path, "ACME-INTERNAL")
    rc = gate.main(["--workdir", str(repo), "--base", base, "--denylist", str(dl)])
    assert rc == 1


def test_feature_branch_upstream_does_not_narrow_the_scan(tmp_path):
    # A pushed feature branch tracks origin/feature. Its merge-base with HEAD is
    # HEAD itself, so using the upstream as the base scans an empty diff and
    # reports clean while an earlier commit of the PR leaks.
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    repo = _repo(tmp_path)
    _git(repo, "branch", "-m", "main")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "checkout", "-q", "-b", "feature")
    _feature_commit(repo, "b.txt", "leak: ACME-INTERNAL host\n")
    _feature_commit(repo, "c.txt", "an ordinary later commit\n")
    _git(repo, "push", "-q", "-u", "origin", "feature")
    dl = _denylist(tmp_path, "ACME-INTERNAL")
    assert gate.main(["--workdir", str(repo), "--denylist", str(dl)]) == 1


def test_default_base_skips_refs_that_already_contain_head(tmp_path):
    # On a fork's pushed default branch, origin/HEAD and origin/main point at
    # HEAD itself, so `base...HEAD` is empty. Such a base cannot be the PR's
    # target; with nothing else to compare against the gate must fail closed.
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    repo = _repo(tmp_path)
    _git(repo, "branch", "-m", "main")
    _feature_commit(repo, "b.txt", "leak: ACME-INTERNAL host\n")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "main")
    _git(repo, "remote", "set-head", "origin", "main")
    dl = _denylist(tmp_path, "ACME-INTERNAL")
    assert gate.main(["--workdir", str(repo), "--denylist", str(dl)]) == 2


def test_default_scan_includes_an_upstream_remote_for_fork_prs(tmp_path):
    # Fork layout: `upstream` is the project the PR targets, `origin` the fork
    # whose main already carries the leaking commit.
    up = tmp_path / "upstream.git"
    fork = tmp_path / "fork.git"
    for bare in (up, fork):
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    repo = _repo(tmp_path)
    _git(repo, "branch", "-m", "main")
    _git(repo, "remote", "add", "upstream", str(up))
    _git(repo, "push", "-q", "upstream", "main")
    _feature_commit(repo, "b.txt", "leak: ACME-INTERNAL host\n")
    _git(repo, "remote", "add", "origin", str(fork))
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "fetch", "-q", "upstream")
    _git(repo, "checkout", "-q", "-b", "feature")
    _feature_commit(repo, "c.txt", "an ordinary later commit\n")
    dl = _denylist(tmp_path, "ACME-INTERNAL")
    assert gate.main(["--workdir", str(repo), "--denylist", str(dl)]) == 1


def test_default_scan_covers_every_candidate_target(tmp_path):
    # The PR targets origin/main at B, while upstream/main is at B -> L (L leaks)
    # and HEAD is B -> L -> F. Scanning against upstream/main alone would see
    # only F; the PR to origin/main adds L too. Without --base the gate cannot
    # know which target is meant, so it scans against every candidate.
    up = tmp_path / "upstream.git"
    fork = tmp_path / "fork.git"
    for bare in (up, fork):
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    repo = _repo(tmp_path)
    _git(repo, "branch", "-m", "main")
    _git(repo, "remote", "add", "origin", str(fork))
    _git(repo, "push", "-q", "origin", "main")
    _feature_commit(repo, "b.txt", "leak: ACME-INTERNAL host\n")
    _git(repo, "remote", "add", "upstream", str(up))
    _git(repo, "push", "-q", "upstream", "main")
    _git(repo, "fetch", "-q", "upstream")
    _git(repo, "checkout", "-q", "-b", "feature")
    _feature_commit(repo, "c.txt", "an ordinary later commit\n")
    dl = _denylist(tmp_path, "ACME-INTERNAL")
    assert gate.main(["--workdir", str(repo), "--denylist", str(dl)]) == 1


@pytest.mark.parametrize("rc", [1, 0])  # a failed call, or no sha printed
def test_default_bases_candidate_without_merge_base_fails_closed(monkeypatch, rc):
    # A shallow clone can hold upstream/main without a computable merge-base.
    # Dropping it silently would let the other candidates' clean scans stand in
    # for the target that was never compared. Break killed: `or` -> `and`.
    fake = _FakeGit(
        {"upstream/main", "origin/main"},
        no_merge_base={"upstream/main"},
        no_merge_base_rc=rc,
    )
    monkeypatch.setattr(gate, "_git", fake)
    with pytest.raises(gate.ScrubError):
        gate.default_bases(Path("/nowhere"))


def test_added_line_starting_with_plus_plus_is_scanned(tmp_path):
    # A content line `++X` shows in the diff as `+++X`, the same prefix as the
    # `+++ b/file` header. Only the header may be skipped.
    repo = _repo(tmp_path)
    base = _feature_commit(repo, "b.txt", "++ACME-INTERNAL\n")
    dl = _denylist(tmp_path, "ACME-INTERNAL")
    rc = gate.main(["--workdir", str(repo), "--base", base, "--denylist", str(dl)])
    assert rc == 1



def test_scan_ignores_a_forced_color_config(tmp_path):
    # `color.diff=always` wraps every diff line in escape codes; a parser that
    # reads the colored text finds no `@@` or `+` and would report clean.
    repo = _repo(tmp_path)
    _git(repo, "config", "color.diff", "always")
    base = _feature_commit(repo, "b.txt", "leak: ACME-INTERNAL host\n")
    dl = _denylist(tmp_path, "ACME-INTERNAL")
    rc = gate.main(["--workdir", str(repo), "--base", base, "--denylist", str(dl)])
    assert rc == 1


def test_textconv_output_of_a_binary_document_is_scanned(tmp_path):
    # With a textconv driver configured, the converted text is the only view of
    # a binary document the scan gets; it must stay on.
    repo = _repo(tmp_path)
    conv = tmp_path / "conv.sh"
    conv.write_text("#!/bin/sh\ntr -d '\\000' < \"$1\"\n")
    conv.chmod(0o755)
    (repo / ".gitattributes").write_text("*.bin diff=nulstrip\n")
    _git(repo, "config", "diff.nulstrip.textconv", str(conv))
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-q", "-m", "attrs")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "doc.bin").write_bytes(b"\x00leak: ACME-INTERNAL host\x00\n")
    _git(repo, "add", "doc.bin")
    _git(repo, "commit", "-q", "-m", "binary doc")
    dl = _denylist(tmp_path, "ACME-INTERNAL")
    rc = gate.main(["--workdir", str(repo), "--base", base, "--denylist", str(dl)])
    assert rc == 1
