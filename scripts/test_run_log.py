"""Tests for run_log.sh — concrete run-log path init (loop #386)."""

import re
import subprocess
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).parent / "run_log.sh"

# .goodfellow/runs/<UTC timestamp>.jsonl — no literal "<timestamp>" placeholder.
PATH_RE = re.compile(r"/\.goodfellow/runs/\d{8}T\d{6}Z\.jsonl$")


def _run(project_root):
    return subprocess.run(
        ["bash", str(SCRIPT), str(project_root)],
        capture_output=True,
        text=True,
        check=True,
    )


def test_prints_concrete_timestamped_path():
    with tempfile.TemporaryDirectory() as d:
        out = _run(d).stdout.strip()
        assert PATH_RE.search(out), f"unexpected path: {out!r}"
        assert "<timestamp>" not in out


def test_creates_runs_dir_but_no_file():
    with tempfile.TemporaryDirectory() as d:
        out = _run(d).stdout.strip()
        assert (Path(d) / ".goodfellow" / "runs").is_dir()
        # File is created only on first append, not by the helper.
        assert not Path(out).exists()


def test_gitignores_goodfellow():
    with tempfile.TemporaryDirectory() as d:
        _run(d)
        gitignore = (Path(d) / ".gitignore").read_text()
        assert ".goodfellow/" in gitignore.splitlines()


def test_path_is_appendable():
    with tempfile.TemporaryDirectory() as d:
        out = _run(d).stdout.strip()
        # The emitted path must be writable by a shell append redirect.
        with open(out, "a") as fh:
            fh.write('{"event": "test"}\n')
        assert Path(out).read_text().strip() == '{"event": "test"}'
