"""Behavioral tests for codex-bridge.sh — the two-reviewer adversarial bridge.

Stubs `codex` and `claude` on PATH (temp dir prepended) so the constructed argv
is captured null-separated. Asserts the review-correctness contract:

  1. No Claude model name (sonnet/opus/haiku) ever reaches `codex exec`.
     $GOODFELLOW_CODEX_MODEL (a GPT id) is the ONLY thing that adds --model to
     the codex path; $GOODFELLOW_REVIEW_MODEL stays on the Claude fallback.
  2. --file mode embeds the actual file body into the codex prompt (so a
     freshly-written, still-untracked spec/plan is actually reviewed, not an
     empty git diff).
"""

import os
import stat
import subprocess
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).parent / "codex-bridge.sh"

CLAUDE_MODEL_NAMES = ("sonnet", "opus", "haiku")

CODEX_STUB = """#!/usr/bin/env bash
if [[ "$1" == "--version" ]]; then
  echo "codex-cli 0.121.0"
  exit 0
fi
printf '%s\\0' "$@" > "$GF_CODEX_ARGV"
cat >/dev/null 2>&1 || true
echo "STUB CODEX REVIEW OUTPUT"
exit 0
"""

CLAUDE_STUB = """#!/usr/bin/env bash
printf '%s\\0' "$@" > "$GF_CLAUDE_ARGV"
cat >/dev/null 2>&1 || true
echo "STUB CLAUDE REVIEW OUTPUT"
exit 0
"""


def _write_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class Bridge:
    """One bridge invocation with stubbed codex/claude and captured argv."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.bindir = tmp / "bin"
        self.bindir.mkdir()
        _write_stub(self.bindir / "codex", CODEX_STUB)
        _write_stub(self.bindir / "claude", CLAUDE_STUB)
        self.codex_argv_file = tmp / "codex_argv"
        self.claude_argv_file = tmp / "claude_argv"

    def run(self, args, env=None):
        run_env = {
            **os.environ,
            "PATH": f"{self.bindir}:{os.environ['PATH']}",
            "GF_CODEX_ARGV": str(self.codex_argv_file),
            "GF_CLAUDE_ARGV": str(self.claude_argv_file),
            **(env or {}),
        }
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            cwd=str(self.tmp),
            stdin=subprocess.DEVNULL,
            env=run_env,
        )

    def codex_argv(self):
        raw = self.codex_argv_file.read_bytes()
        return [a.decode() for a in raw.split(b"\0") if a]

    def claude_argv(self):
        raw = self.claude_argv_file.read_bytes()
        return [a.decode() for a in raw.split(b"\0") if a]


def _spec_file(tmp: Path, content: str) -> Path:
    f = tmp / "my-spec.md"
    f.write_text(content)
    return f


# --- Bug 1: no Claude model name reaches codex exec -------------------------


def test_codex_path_never_receives_claude_model_name():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        spec = _spec_file(tmp, "# Spec\nSome requirement.\n")
        b = Bridge(tmp)
        # Default GOODFELLOW_REVIEW_MODEL is "sonnet" — must NOT leak to codex.
        r = b.run(
            ["--kind", "spec", "--file", str(spec)],
            env={"GOODFELLOW_REVIEW_MODEL": "sonnet"},
        )
        assert r.returncode == 0, r.stderr
        argv = b.codex_argv()
        assert argv[0] == "exec" and argv[1] == "review"
        # No claude model id anywhere in the codex argv.
        joined = " ".join(argv)
        for name in CLAUDE_MODEL_NAMES:
            assert name not in joined, f"claude model '{name}' leaked to codex: {argv}"
        # And --model must not appear at all (no GOODFELLOW_CODEX_MODEL set).
        assert "--model" not in argv


def test_codex_model_only_from_codex_env_var():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        spec = _spec_file(tmp, "# Spec\n")
        b = Bridge(tmp)
        r = b.run(
            ["--kind", "spec", "--file", str(spec)],
            env={
                "GOODFELLOW_REVIEW_MODEL": "opus",
                "GOODFELLOW_CODEX_MODEL": "gpt-5-codex",
            },
        )
        assert r.returncode == 0, r.stderr
        argv = b.codex_argv()
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "gpt-5-codex"
        # The claude model id still must not appear.
        assert "opus" not in " ".join(argv)


# --- Bug 2: --file mode feeds file CONTENT to the reviewer -------------------


def test_file_mode_embeds_file_contents_in_codex_prompt():
    sentinel = "SENTINEL_UNTRACKED_SPEC_BODY_42"
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        spec = _spec_file(tmp, f"# Spec\n{sentinel}\nA requirement.\n")
        b = Bridge(tmp)
        r = b.run(["--kind", "spec", "--file", str(spec)])
        assert r.returncode == 0, r.stderr
        argv = b.codex_argv()
        # File mode uses a positional prompt (no scope flag, no stdin dash).
        assert "--uncommitted" not in argv
        assert "--commit" not in argv
        assert "--base" not in argv
        assert "-" not in argv
        # The actual file body is embedded in the positional prompt argument.
        assert any(sentinel in a for a in argv), (
            f"file body not embedded in codex prompt: {argv}"
        )


def test_file_mode_missing_file_errors():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        b = Bridge(tmp)
        r = b.run(["--kind", "spec", "--file", str(tmp / "does-not-exist.md")])
        assert r.returncode != 0
        assert "not found" in r.stderr.lower()


# --- Scope-flag (diff) path still pipes prompt via stdin dash ----------------


def test_uncommitted_mode_uses_scope_flag_and_stdin_dash():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        b = Bridge(tmp)
        r = b.run(["--kind", "diff", "--uncommitted"])
        assert r.returncode == 0, r.stderr
        argv = b.codex_argv()
        assert "--uncommitted" in argv
        assert "-" in argv  # prompt piped via stdin, dash positional
        assert "--model" not in argv
        for name in CLAUDE_MODEL_NAMES:
            assert name not in " ".join(argv)


# --- Claude fallback still receives the Claude model id ----------------------


def test_claude_fallback_receives_claude_model():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        spec = _spec_file(tmp, "# Spec\nbody\n")
        b = Bridge(tmp)
        # GOODFELLOW_CODEX=0 forces the single-Claude fallback path.
        r = b.run(
            ["--kind", "spec", "--file", str(spec)],
            env={"GOODFELLOW_CODEX": "0", "GOODFELLOW_REVIEW_MODEL": "opus"},
        )
        assert r.returncode == 0, r.stderr
        argv = b.claude_argv()
        assert "--print" in argv
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "opus"
        # Codex must not have been invoked.
        assert not b.codex_argv_file.exists()
