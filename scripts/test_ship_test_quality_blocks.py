"""Run the ship skill's own test-quality commands, as written, against a real repo.

A text-presence check on SKILL.md would pass while the documented command is
broken (wrong path, wrong flag, wrong variable). These tests extract the fenced
bash blocks from ship's "Tests that can fail" section and execute them verbatim
with CLAUDE_PLUGIN_ROOT and BASE set, against a repo with a planted weak test.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
SHIP = ROOT / "skills" / "ship" / "SKILL.md"


def _section_blocks():
    text = SHIP.read_text()
    m = re.search(
        r"^### 1a\. Tests that can fail\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S
    )
    assert m, "ship skill lost its '1a. Tests that can fail' section"
    return re.findall(r"```bash\n(.*?)```", m.group(1), re.S)


def _block(script: str) -> str:
    hits = [b for b in _section_blocks() if script in b]
    assert len(hits) == 1, f"expected one bash block invoking {script}"
    return hits[0]


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _repo(tmp_path, test_body):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "tester@example.com")
    _git(repo, "config", "user.name", "tester")
    (repo / ".gitignore").write_text("__pycache__/\n")
    (repo / "gate.py").write_text("def allowed(n):\n    return True\n")
    _git(repo, "add", ".gitignore", "gate.py")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "gate.py").write_text("def allowed(n):\n    return n >= 10\n")
    (repo / "test_gate.py").write_text(test_body)
    _git(repo, "add", "gate.py", "test_gate.py")
    _git(repo, "commit", "-q", "-m", "gate")
    return repo, base


def _run_block(block, repo, base):
    env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(ROOT), "BASE": base}
    return subprocess.run(
        ["bash", "-c", block], cwd=str(repo), env=env, capture_output=True, text=True
    )


WEAK = "from gate import allowed\n\n\ndef test_small_denied():\n    assert not allowed(1)\n"


def test_red_check_block_runs_and_accepts_an_assertion_red(tmp_path):
    repo, base = _repo(tmp_path, WEAK)
    proc = _run_block(_block("red_check.py"), repo, base)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK" in proc.stdout and "test_small_denied" in proc.stdout


def test_mutation_block_skips_visibly_without_a_path_list(tmp_path):
    repo, base = _repo(tmp_path, WEAK)
    proc = _run_block(_block("mutation_check.py"), repo, base)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SKIPPED" in proc.stderr


def test_mutation_block_reports_the_planted_boundary_survivor(tmp_path):
    repo, base = _repo(tmp_path, WEAK)
    (repo / ".goodfellow").mkdir()
    (repo / ".goodfellow" / "high_stakes_paths.txt").write_text("gate.py\n")
    proc = _run_block(_block("mutation_check.py"), repo, base)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "SURVIVED gate.py:2 cmp_boundary" in proc.stdout
