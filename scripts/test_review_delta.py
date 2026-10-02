"""review_delta.py: which commits the final-HEAD check must review.

After the last review round, `ship` reviews only the commits that came after the
last reviewed one. A rebase rewrites every SHA, so the reviewed commit is no
longer an ancestor of HEAD; diffing against it would review everything the base
branch gained in the meantime, or worse, nothing useful. The mapper finds the
reviewed commit's rebased counterpart by patch-id and file content, and falls
back to the whole branch on any doubt.
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import review_delta  # noqa: E402

SCRIPT = Path(__file__).resolve().parent / "review_delta.py"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo: Path, path: str, text: str, msg: str) -> str:
    p = repo / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    git(r, "config", "commit.gpgsign", "false")
    commit(r, "base.txt", "base\n", "base")
    commit(r, "shared.txt", "".join(f"line{i}\n" for i in range(12)), "shared")
    git(r, "switch", "-q", "-c", "feature")
    commit(r, "a.py", "a = 1\n", "A")
    commit(r, "b.py", "b = 2\n", "B")
    return r


def main_gains(repo: Path, path: str = "upstream.txt", text: str = "up\n") -> None:
    git(repo, "switch", "-q", "main")
    commit(repo, path, text, "upstream work")
    git(repo, "switch", "-q", "feature")


def test_ancestor_reviews_only_the_fix(repo: Path):
    last = git(repo, "rev-parse", "HEAD")
    commit(repo, "c.py", "c = 3\n", "fix")
    d = review_delta.delta_base(repo, last, "main")
    assert (d.mode, d.base) == ("ancestor", last)


def test_nothing_new_is_already_reviewed(repo: Path):
    last = git(repo, "rev-parse", "HEAD")
    d = review_delta.delta_base(repo, last, "main")
    assert d.mode == "reviewed"


def test_rebase_maps_the_reviewed_commit_to_its_counterpart(repo: Path):
    last = git(repo, "rev-parse", "HEAD")
    commit(repo, "c.py", "c = 3\n", "fix")
    main_gains(repo)
    git(repo, "rebase", "-q", "main")
    mapped = git(repo, "rev-parse", "HEAD~1")
    assert mapped != last
    d = review_delta.delta_base(repo, last, "main")
    assert (d.mode, d.base) == ("rebase", mapped), d.reason


def test_pure_rebase_needs_no_review(repo: Path):
    last = git(repo, "rev-parse", "HEAD")
    main_gains(repo)
    git(repo, "rebase", "-q", "main")
    d = review_delta.delta_base(repo, last, "main")
    assert d.mode == "reviewed", d.reason


def test_rewritten_reviewed_commit_reviews_the_whole_branch(repo: Path):
    last = git(repo, "rev-parse", "HEAD")
    main_gains(repo)
    git(repo, "rebase", "-q", "main")
    # amend the (rebased) reviewed commit: its patch is no longer what was reviewed
    (repo / "b.py").write_text("b = 99\n")
    git(repo, "commit", "-q", "-a", "--amend", "--no-edit")
    d = review_delta.delta_base(repo, last, "main")
    assert d.mode == "full"
    assert d.base == git(repo, "merge-base", "HEAD", "main")


def test_reordered_commits_review_the_whole_branch(repo: Path):
    last = git(repo, "rev-parse", "HEAD")
    a, b = git(repo, "rev-parse", "HEAD~1"), last
    git(repo, "reset", "-q", "--hard", "main")
    git(repo, "cherry-pick", b)
    git(repo, "cherry-pick", a)
    d = review_delta.delta_base(repo, last, "main")
    assert d.mode == "full", d.reason


def test_dropped_reviewed_commit_reviews_the_whole_branch(repo: Path):
    last = git(repo, "rev-parse", "HEAD")
    git(repo, "reset", "-q", "--hard", "main")
    git(repo, "cherry-pick", last)  # A is gone
    d = review_delta.delta_base(repo, last, "main")
    assert d.mode == "full", d.reason


def test_merge_commit_reviews_the_whole_branch(repo: Path):
    last = git(repo, "rev-parse", "HEAD")
    main_gains(repo)
    git(repo, "rebase", "-q", "main")
    git(repo, "switch", "-q", "-c", "side", "main")
    commit(repo, "side.txt", "s\n", "side")
    git(repo, "switch", "-q", "feature")
    git(repo, "merge", "-q", "--no-ff", "--no-edit", "side")
    d = review_delta.delta_base(repo, last, "main")
    assert d.mode == "full" and "merge" in d.reason


def test_unknown_last_sha_reviews_the_whole_branch(repo: Path):
    d = review_delta.delta_base(repo, "0" * 40, "main")
    assert d.mode == "full"


def test_same_patch_on_changed_file_content_reviews_the_whole_branch(repo: Path):
    """The patch-id ignores where a hunk lands; the mapping must also prove the
    reviewed files are byte-identical. Here main changed the same file, so the
    rebased commit applies the same hunk to different surroundings."""
    lines = [f"line{i}\n" for i in range(12)]
    commit(
        repo,
        "shared.txt",
        "".join(lines[:1] + ["CHANGED\n"] + lines[2:]),
        "edit shared",
    )
    last = git(repo, "rev-parse", "HEAD")
    main_gains(repo, "shared.txt", "".join(lines[:11] + ["upstream\n"]))
    git(repo, "rebase", "-q", "main")
    d = review_delta.delta_base(repo, last, "main")
    assert d.mode == "full" and "shared.txt" in d.reason


def test_cli_prints_base_and_mode(repo: Path):
    last = git(repo, "rev-parse", "HEAD")
    commit(repo, "c.py", "c = 3\n", "fix")
    p = subprocess.run(
        [sys.executable, str(SCRIPT), "--last", last, "--base", "main"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert p.returncode == 0, p.stderr
    base, mode = p.stdout.split()[:2]
    assert (base, mode) == (last, "ancestor")


def test_cli_fails_closed_on_a_bad_base(repo: Path):
    last = git(repo, "rev-parse", "HEAD")
    p = subprocess.run(
        [sys.executable, str(SCRIPT), "--last", last, "--base", "no-such-branch"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert p.returncode == 2
    assert p.stdout == ""


def test_ship_final_head_check_uses_the_mapper():
    """The final-HEAD check lives in skill prose; pin the wiring."""
    ship = (
        Path(__file__).resolve().parents[1] / "skills" / "ship" / "SKILL.md"
    ).read_text()
    section = ship.split("### Final-HEAD check", 1)[1].split("\n## ", 1)[0]
    assert "scripts/review_delta.py" in section
    assert '--base "$DELTA"' in section
    assert "DELTA=$BASE MODE=full" in section, (
        "a mapper failure must review the whole branch"
    )


def _final_head_block() -> str:
    ship = (
        Path(__file__).resolve().parents[1] / "skills" / "ship" / "SKILL.md"
    ).read_text()
    section = ship.split("### Final-HEAD check", 1)[1].split("\n## ", 1)[0]
    return section.split("```bash\n", 1)[1].split("```", 1)[0]


def _run_final_head(
    repo: Path, tmp_path: Path, last: str, base: str, mapper_fails: bool = False
) -> tuple[int, str]:
    """Execute the skill's own final-HEAD shell block with a stub review bridge
    that records the base it was asked to review."""
    root = tmp_path / "plugin"
    (root / "scripts").mkdir(parents=True)
    if mapper_fails:
        (root / "scripts" / "review_delta.py").write_text("import sys\nsys.exit(2)\n")
    else:
        (root / "scripts" / "review_delta.py").symlink_to(SCRIPT)
    calls = tmp_path / "bridge-calls"
    # Like the real bridge, refuse a base that does not resolve: a fallback to
    # an unusable base must fail here, not pass.
    (root / "scripts" / "codex-bridge.sh").write_text(
        "#!/bin/bash\n"
        'git rev-parse --verify --quiet "$4^{commit}" >/dev/null || '
        '{ echo "REVIEW_FAILED 2 bad-base"; exit 2; }\n'
        f'echo "$*" >> {calls}\necho /tmp/review-artifact.md\n'
    )
    block = _final_head_block().replace("<last-reviewed-sha>", last)
    p = subprocess.run(
        ["bash", "-c", block],
        cwd=repo,
        env={**__import__("os").environ, "CLAUDE_PLUGIN_ROOT": str(root), "BASE": base},
        capture_output=True,
        text=True,
    )
    return p.returncode, calls.read_text() if calls.exists() else ""


def test_final_head_block_reviews_only_the_fix(repo: Path, tmp_path: Path):
    last = git(repo, "rev-parse", "HEAD")
    commit(repo, "c.py", "c = 3\n", "fix")
    rc, calls = _run_final_head(repo, tmp_path, last, "main")
    assert rc == 0
    assert calls.split() == ["--kind", "diff", "--base", last]


def test_final_head_block_reviews_the_rebased_delta(repo: Path, tmp_path: Path):
    last = git(repo, "rev-parse", "HEAD")
    commit(repo, "c.py", "c = 3\n", "fix")
    main_gains(repo)
    git(repo, "rebase", "-q", "main")
    rc, calls = _run_final_head(repo, tmp_path, last, "main")
    assert rc == 0
    assert calls.split() == [
        "--kind",
        "diff",
        "--base",
        git(repo, "rev-parse", "HEAD~1"),
    ]


def test_final_head_block_skips_only_when_nothing_is_new(repo: Path, tmp_path: Path):
    last = git(repo, "rev-parse", "HEAD")
    rc, calls = _run_final_head(repo, tmp_path, last, "main")
    assert rc == 0 and calls == ""


def test_final_head_block_reviews_everything_when_the_mapper_fails(
    repo: Path, tmp_path: Path
):
    last = git(repo, "rev-parse", "HEAD")
    commit(repo, "c.py", "c = 3\\n", "fix")
    rc, calls = _run_final_head(repo, tmp_path, last, "main", mapper_fails=True)
    assert rc == 0
    assert calls.split() == ["--kind", "diff", "--base", "main"]


def test_leading_space_filename_is_compared_exactly(repo: Path):
    """Raw -z output: a reviewed file named ' secret.py' must not be compared
    under a stripped name that matches nothing."""
    lines = [f"line{i}\n" for i in range(12)]
    git(repo, "switch", "-q", "main")
    commit(repo, " secret.py", "".join(lines), "add secret")
    git(repo, "switch", "-q", "feature")
    git(repo, "rebase", "-q", "main")
    commit(repo, " secret.py", "".join(lines[:1] + ["CHANGED\n"] + lines[2:]), "edit")
    last = git(repo, "rev-parse", "HEAD")
    main_gains(repo, " secret.py", "".join(lines[:11] + ["upstream\n"]))
    git(repo, "rebase", "-q", "main")
    d = review_delta.delta_base(repo, last, "main")
    assert d.mode == "full" and " secret.py" in d.reason, d
