"""Deterministic contract tests for review_context.py.

Uses temporary git fixture repos in tmp_path (OS temp — no workspace-root leak).
No real Codex or network calls.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from review_context import _parse_changed, assemble_context


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _init(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tester@example.com")
    _git(root, "config", "user.name", "tester")


def _commit(root: Path, files: dict[str, str], msg: str) -> str:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git(root, "add", *files.keys())
    _git(root, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


# ---------------------------------------------------------------------------
# Per-mode full-file + deletions + budget
# ---------------------------------------------------------------------------


def test_commit_era_bytes_not_head(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init(repo)
    sha1 = _commit(repo, {"X.py": "V1_commit_era = 1\n"}, "c1")
    _commit(repo, {"X.py": "V2_head_bytes = 2\n"}, "c2")

    out = assemble_context(
        mode="commit",
        workdir=repo,
        rev=sha1,
        changed_files=[("A", "X.py")],
        diff_range=sha1,
    )
    assert "V1_commit_era" in out
    assert "V2_head_bytes" not in out


def test_deletion_completes_with_note(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init(repo)
    _commit(repo, {"keep.py": "keep = 1\n", "gone.py": "gone = 1\n"}, "c1")
    _git(repo, "rm", "-q", "gone.py")
    _git(repo, "commit", "-q", "-m", "delete gone")
    sha2 = _git(repo, "rev-parse", "HEAD")

    out = assemble_context(
        mode="commit",
        workdir=repo,
        rev=sha2,
        changed_files=[("D", "gone.py")],
        diff_range=sha2,
    )
    assert "gone.py — deleted in this change (no full-file context)" in out


def test_oversize_file_omitted_not_truncated(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init(repo)
    big = "A" * 200 + "\n"
    sha = _commit(repo, {"big.py": big}, "c1")

    out = assemble_context(
        mode="commit",
        workdir=repo,
        rev=sha,
        changed_files=[("A", "big.py")],
        diff_range=sha,
        fullfile_budget=50,
    )
    assert "Context-omitted (Read on demand): big.py" in out
    assert "A" * 60 not in out  # not truncated mid-file — omitted whole


# ---------------------------------------------------------------------------
# Token cross-ref: coupling, mode-scope, determinism, option-safety
# ---------------------------------------------------------------------------


def test_coupling_via_token_not_caller_grep(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init(repo)
    _commit(repo, {"B.tsx": '<div className="text-destructive">x</div>\n'}, "base")
    _commit(repo, {"A.tsx": '<span className="text-destructive" />\n'}, "add A")

    out = assemble_context(
        mode="diff",
        workdir=repo,
        rev="HEAD",
        changed_files=[("A", "A.tsx")],
        diff_range="HEAD~1",
    )
    # coupling surfaced via token cross-ref (A.tsx exports nothing → caller-grep can't).
    assert "text-destructive" in out
    assert "B.tsx:1" in out
    assert "## Exported-symbol callers" not in out


def test_commit_era_token_scope(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init(repo)
    # B references the token at line 3 initially.
    _commit(repo, {"B.tsx": 'a\nb\n<i className="text-destructive"/>\n'}, "base B")
    sha2 = _commit(
        repo, {"A.tsx": '<span className="text-destructive"/>\n'}, "reviewed A"
    )
    # After the reviewed commit, B moves the token to line 5.
    _commit(
        repo, {"B.tsx": 'a\nb\nc\nd\n<i className="text-destructive"/>\n'}, "move B"
    )

    out = assemble_context(
        mode="commit",
        workdir=repo,
        rev=sha2,
        changed_files=[("A", "A.tsx")],
        diff_range=sha2,
    )
    assert "B.tsx:3" in out  # commit-era line
    assert "B.tsx:5" not in out  # not HEAD line


def test_determinism_priority_and_overflow(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init(repo)
    _commit(repo, {"B.tsx": '<div className="text-destructive"/>\n'}, "base B")
    # A: one class-2 utility token + 40 class-3 route strings → 41 candidates.
    lines = ['<span className="text-destructive"/>']
    for i in range(40):
        lines.append(f'const p{i:02d} = "/route-a{i:02d}";')
    _commit(repo, {"A.tsx": "\n".join(lines) + "\n"}, "add A")

    kwargs = dict(
        mode="diff",
        workdir=repo,
        rev="HEAD",
        changed_files=[("A", "A.tsx")],
        diff_range="HEAD~1",
    )
    out1 = assemble_context(**kwargs)
    out2 = assemble_context(**kwargs)

    # priority: class-2 utility token kept over class-3 flood, and it surfaces via B.
    assert "text-destructive" in out1
    assert "B.tsx:1" in out1
    # 41 candidates, cap 30 → 11 overflow.
    assert "+11 more token cross-refs" in out1
    # determinism
    assert out1 == out2


def test_option_safe_grep_css_custom_prop(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init(repo)
    _commit(repo, {"B.css": ".b { color: var(--surface-canvas); }\n"}, "base B")
    _commit(repo, {"A.css": ".a { background: var(--surface-canvas); }\n"}, "add A")

    # Must not raise / must not exit 129 on the `--`-prefixed token.
    out = assemble_context(
        mode="diff",
        workdir=repo,
        rev="HEAD",
        changed_files=[("A", "A.css")],
        diff_range="HEAD~1",
    )
    assert "--surface-canvas" in out
    assert "B.css:1" in out


# --- rename-status parsing ---------------------------------------------------


def test_parse_changed_rename_uses_target_path() -> None:
    """git --name-status emits `R100\\tsrc\\tdst`; the TARGET (last field) is the
    reviewed path. A first-tab split would leave `src\\tdst`, so `git show
    <rev>:<that>` fails and the renamed file drops from context."""
    parsed = _parse_changed(["R100\told/name.py\tnew/name.py"])
    assert parsed == [("R100", "new/name.py")]


def test_parse_changed_plain_and_colon_forms_unchanged() -> None:
    assert _parse_changed(["M\tpkg/a.py"]) == [("M", "pkg/a.py")]
    assert _parse_changed(["A:pkg/b.py"]) == [("A", "pkg/b.py")]


def test_renamed_target_inlined_in_context(tmp_path: Path) -> None:
    """End-to-end: a renamed file's NEW path is inlined as full-file context."""
    repo = tmp_path / "repo"
    _init(repo)
    _commit(repo, {"old_mod.py": "def f():\n    return 1\n"}, "base")
    # Rename old_mod.py -> new_mod.py and mutate slightly so rename detection fires.
    _git(repo, "mv", "old_mod.py", "new_mod.py")
    (repo / "new_mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "rename+edit")
    name_status = _git(repo, "diff", "--name-status", "HEAD~1...HEAD")
    changed = _parse_changed(name_status.splitlines())
    # The parsed target must be the NEW path, readable at HEAD.
    assert any(path == "new_mod.py" for _s, path in changed)
    out = assemble_context(
        mode="diff",
        workdir=repo,
        rev="HEAD",
        changed_files=changed,
        diff_range="HEAD~1...HEAD",
    )
    assert "new_mod.py" in out
    assert "return 2" in out  # full-file body of the renamed target inlined
