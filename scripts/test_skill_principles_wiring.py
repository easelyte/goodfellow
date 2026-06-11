"""Assert all five chain skills wire in the principles read-path (CM1).

A missed skill can't ship silently: every chain skill that reads knowledge must
invoke the principles_context resolver.
"""

import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SK = ROOT / "skills"


def _extract_resolver_block(skill_md_text):
    """Return the fenced ```bash block that invokes principles_context, or None."""
    for m in re.finditer(r"```bash\n(.*?)```", skill_md_text, re.S):
        if "principles_context.py" in m.group(1):
            return m.group(1)
    return None


CHAIN_SKILLS = ("brainstorm", "spec-review", "plan", "plan-review", "execute")


def test_all_five_skills_invoke_principles_context():
    for s in CHAIN_SKILLS:
        txt = (SK / s / "SKILL.md").read_text()
        assert "principles_context.py" in txt, f"{s} missing principles read-path"


def test_skills_use_single_emit_command_not_inline_bash():
    """Post-refactor contract: each skill invokes the ONE robust helper (`--emit`)
    and contains NONE of the old hand-rolled inline bash (which twice shipped a
    silent-failure bug — CB1 swallowed the resolver exit, R6 swallowed a cat).
    All error handling now lives in Python, so the skill markdown can't reintroduce
    the class."""
    for s in CHAIN_SKILLS:
        txt = (SK / s / "SKILL.md").read_text()
        assert "principles_context.py" in txt and "--emit" in txt, (
            f"{s} does not invoke the --emit helper"
        )
        # the fragile inline patterns must be gone for good
        assert "for f in " not in txt, f"{s} still has an inline read loop"
        assert "principle_files=$(" not in txt, f"{s} still has inline capture bash"


def test_emit_block_hard_errors_end_to_end(tmp_path):
    """Behavioral guard: extract each skill's real bash block and RUN it under two
    failure modes — invalid env, and missing core seed. Both must exit non-zero
    (the helper centralizes the failure handling). Proves the contract end-to-end,
    not just by source-string inspection."""
    # fake plugin root with a working copy of the resolver + a core seed
    plugin = tmp_path / "plugin"
    (plugin / "scripts").mkdir(parents=True)
    (plugin / "knowledge").mkdir()
    (plugin / "scripts" / "principles_context.py").write_text(
        (ROOT / "scripts" / "principles_context.py").read_text()
    )
    (plugin / "knowledge" / "principles.md").write_text("core")
    proj = tmp_path / "proj"
    proj.mkdir()

    def run(block, **extra_env):
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("GOODFELLOW_PRINCIPLES_WEB",)
        }
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin)
        env.update(extra_env)
        return subprocess.run(
            ["bash", "-c", block], cwd=proj, env=env, capture_output=True, text=True
        )

    for s in CHAIN_SKILLS:
        block = _extract_resolver_block((SK / s / "SKILL.md").read_text())
        assert block, f"{s} has no runnable principles_context bash block"
        # 1) invalid env -> exit non-zero
        assert run(block, GOODFELLOW_PRINCIPLES_WEB="true").returncode != 0, (
            f"{s} did not hard-error on invalid env"
        )
        # 2) happy path -> exit 0 and emits the core content
        ok = run(block)
        assert ok.returncode == 0 and "core" in ok.stdout, f"{s} happy path broken"
        # 3) missing core seed -> exit non-zero (no silent core loss, R6 class)
        (plugin / "knowledge" / "principles.md").unlink()
        assert run(block).returncode != 0, f"{s} silently tolerated missing core seed"
        (plugin / "knowledge" / "principles.md").write_text("core")  # restore
