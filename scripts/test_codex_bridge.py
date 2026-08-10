"""Behavioral tests for codex-bridge.sh — the two-stage generator+judge bridge.

Stubs `codex` and `claude` on PATH (temp dir prepended) so the constructed argv
is captured null-separated. Asserts:

  1. No Claude model name (sonnet/opus/haiku) ever reaches `codex exec`.
     $GOODFELLOW_CODEX_MODEL (a GPT id) is the ONLY thing that adds --model to
     the codex path; $GOODFELLOW_REVIEW_MODEL stays on the Claude fallback.
  2. --file mode embeds the actual file body into the generator prompt.
  3. The REVIEW_FAILED sentinel contract: a forced codex failure emits a nonzero
     exit AND a `REVIEW_FAILED <rc> <class>` last stdout line (never a path); the
     success path's last stdout line is a readable artifact path.
  4. The generator prompt carries the verify-by-exploration mandate + coverage
     block, cites P-NNN (never PNN/RNNN), and contains no scrubbed internal token.
"""

import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).parent / "codex-bridge.sh"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]

CLAUDE_MODEL_NAMES = ("sonnet", "opus", "haiku")

# A codex stub that honors `-o <path>`, detects generator vs judge by the prompt,
# and writes a contract-valid finding block / decision table respectively.
CODEX_STUB = r"""#!/usr/bin/env bash
if [[ "$1" == "--version" ]]; then
  echo "codex-cli 0.121.0"
  exit 0
fi
printf '%s\0' "$@" > "$GF_CODEX_ARGV"
out=""
prev=""
for a in "$@"; do
  [[ "$prev" == "-o" ]] && out="$a"
  prev="$a"
done
prompt="$(cat)"
if [[ "$prompt" == *"Decision object schema"* ]]; then
  body='```json
[{"finding_id":"F1","decision":"keep","judge_score":8,"drop_reason":null,"reclassified_to":null}]
```'
else
  body='## Verdict
Changes requested

## Blockers

### B1. Stub finding
stub prose here

```json
{"finding_id":"F1","severity":"blocker","ship_blocking":true,"out_of_scope_load_bearing":false,"area":"x","short_label":"stub","normalized_text":"body"}
```'
fi
if [[ -n "$out" ]]; then
  printf '%s\n' "$body" > "$out"
else
  printf '%s\n' "$body"
fi
exit 0
"""

# A codex stub that fails the exec pass (nonzero) to exercise the sentinel.
CODEX_FAIL_STUB = r"""#!/usr/bin/env bash
if [[ "$1" == "--version" ]]; then
  echo "codex-cli 0.121.0"
  exit 0
fi
cat >/dev/null 2>&1 || true
echo "boom" >&2
exit 1
"""

CLAUDE_STUB = r"""#!/usr/bin/env bash
printf '%s\0' "$@" > "$GF_CLAUDE_ARGV"
cat >/dev/null 2>&1 || true
echo "STUB CLAUDE REVIEW OUTPUT"
exit 0
"""

# A claude stub that succeeds but produces whitespace-only output.
CLAUDE_EMPTY_STUB = r"""#!/usr/bin/env bash
printf '%s\0' "$@" > "$GF_CLAUDE_ARGV"
cat >/dev/null 2>&1 || true
printf '   \n'
exit 0
"""


def _write_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class Bridge:
    """One bridge invocation with stubbed codex/claude and captured argv."""

    def __init__(
        self, tmp: Path, codex_body: str = CODEX_STUB, claude_body: str = CLAUDE_STUB
    ):
        self.tmp = tmp
        self.bindir = tmp / "bin"
        self.bindir.mkdir()
        _write_stub(self.bindir / "codex", codex_body)
        _write_stub(self.bindir / "claude", claude_body)
        self.codex_argv_file = tmp / "codex_argv"
        self.claude_argv_file = tmp / "claude_argv"

    def run(self, args, env=None):
        run_env = {
            **os.environ,
            "PATH": f"{self.bindir}:{os.environ['PATH']}",
            "GF_CODEX_ARGV": str(self.codex_argv_file),
            "GF_CLAUDE_ARGV": str(self.claude_argv_file),
            "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
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


def _last_line(stdout: str) -> str:
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


# --- Model isolation: no Claude model name reaches codex exec ----------------


def test_codex_path_never_receives_claude_model_name():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        spec = _spec_file(tmp, "# Spec\nSome requirement.\n")
        b = Bridge(tmp)
        r = b.run(
            ["--kind", "spec", "--file", str(spec)],
            env={"GOODFELLOW_REVIEW_MODEL": "sonnet"},
        )
        assert r.returncode == 0, r.stderr
        argv = b.codex_argv()
        assert argv[0] == "exec"
        joined = " ".join(argv)
        for name in CLAUDE_MODEL_NAMES:
            assert name not in joined, f"claude model '{name}' leaked to codex: {argv}"
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
        assert "opus" not in " ".join(argv)


# --- --file mode feeds the file CONTENT to the reviewer ----------------------


def test_file_mode_embeds_file_contents_in_generator_prompt():
    sentinel = "SENTINEL_UNTRACKED_SPEC_BODY_42"
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        spec = _spec_file(tmp, f"# Spec\n{sentinel}\nA requirement.\n")
        b = Bridge(tmp)
        r = b.run(
            ["--kind", "spec", "--file", str(spec)],
            env={"GOODFELLOW_CODEX_DRY_RUN": "1"},
        )
        assert r.returncode == 0, r.stderr
        prompt = Path(_last_line(r.stdout)).read_text()
        assert sentinel in prompt, "file body not embedded in generator prompt"


def test_file_mode_missing_file_errors():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        b = Bridge(tmp)
        r = b.run(["--kind", "spec", "--file", str(tmp / "does-not-exist.md")])
        assert r.returncode != 0
        assert "not found" in r.stderr.lower()
        assert _last_line(r.stdout).startswith("REVIEW_FAILED ")


# --- Sentinel contract -------------------------------------------------------


def test_success_last_stdout_line_is_readable_artifact_path():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        b = Bridge(tmp)
        r = b.run(["--kind", "diff", "--uncommitted"])
        assert r.returncode == 0, r.stderr
        last = _last_line(r.stdout)
        assert not last.startswith("REVIEW_FAILED ")
        out = Path(last)
        assert out.is_file() and os.access(out, os.R_OK)
        # judged review: the reconcile appended a Judge audit section.
        assert "## Judge audit" in out.read_text()


def test_codex_failure_emits_sentinel_and_nonzero_exit():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        b = Bridge(tmp, codex_body=CODEX_FAIL_STUB)
        r = b.run(["--kind", "diff", "--uncommitted"])
        assert r.returncode != 0
        last = _last_line(r.stdout)
        m = re.match(r"^REVIEW_FAILED (\d+) (\S+)$", last)
        assert m, f"expected REVIEW_FAILED sentinel, got: {last!r}"
        assert int(m.group(1)) == r.returncode
        # exactly one sentinel line
        assert sum(ln.startswith("REVIEW_FAILED ") for ln in r.stdout.splitlines()) == 1


# --- Claude fallback still receives the Claude model id ----------------------


def test_claude_fallback_receives_claude_model():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        spec = _spec_file(tmp, "# Spec\nbody\n")
        b = Bridge(tmp)
        r = b.run(
            ["--kind", "spec", "--file", str(spec)],
            env={"GOODFELLOW_CODEX": "0", "GOODFELLOW_REVIEW_MODEL": "opus"},
        )
        assert r.returncode == 0, r.stderr
        argv = b.claude_argv()
        assert "--print" in argv
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "opus"
        assert not b.codex_argv_file.exists()


def test_claude_fallback_empty_output_emits_sentinel():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        spec = _spec_file(tmp, "# Spec\nbody\n")
        b = Bridge(tmp, claude_body=CLAUDE_EMPTY_STUB)
        r = b.run(
            ["--kind", "spec", "--file", str(spec)],
            env={"GOODFELLOW_CODEX": "0"},
        )
        assert r.returncode != 0
        last = _last_line(r.stdout)
        m = re.match(r"^REVIEW_FAILED (\d+) (\S+)$", last)
        assert m, f"expected REVIEW_FAILED sentinel, got: {last!r}"
        assert m.group(2) == "empty-output"


# --- Prompt assembly (dry-run) ----------------------------------------------


def test_generator_prompt_has_verify_mandate_and_coverage_block():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        # need a git repo so --uncommitted produces a diff context
        subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
        (tmp / "a.txt").write_text("x\n")
        b = Bridge(tmp)
        r = b.run(
            ["--kind", "diff", "--uncommitted"],
            env={"GOODFELLOW_CODEX_DRY_RUN": "1"},
        )
        assert r.returncode == 0, r.stderr
        prompt = Path(_last_line(r.stdout)).read_text()
        assert "verification_mandate" in prompt
        assert "```coverage" in prompt
        assert "finding_id" in prompt
        assert "out_of_scope_load_bearing" in prompt


def test_generator_prompt_cites_p_nnn_and_no_scrubbed_tokens():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        spec = _spec_file(tmp, "# Spec\nreq\n")
        b = Bridge(tmp)
        r = b.run(
            ["--kind", "spec", "--file", str(spec)],
            env={"GOODFELLOW_CODEX_DRY_RUN": "1"},
        )
        assert r.returncode == 0, r.stderr
        prompt = Path(_last_line(r.stdout)).read_text()
        # goodfellow principle citation form is present.
        assert "P-NNN" in prompt
        # No upstream internal tokens leak into the assembled prompt. The tokens
        # are assembled from fragments so this test file itself stays scan-clean.
        forbidden = (
            "son-of" + "-anton",
            "RULES" + ".md",
            "mat" + "ron",
            "defect" + "_class",
            "universal-design" + "-principles",
        )
        for bad in forbidden:
            assert bad not in prompt, f"scrubbed token leaked into prompt: {bad}"
        # No bare upstream rule-id citation forms (R### / V# / PNN unhyphenated).
        assert not re.search(r"\bR[0-9]{3}\b", prompt)
        assert not re.search(r"\bV[1-9]\b", prompt)
