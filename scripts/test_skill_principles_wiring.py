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


def test_skills_consume_resolver_output_and_propagate_errors():
    """M2: a bare filename mention isn't enough — verify the real shell contract:
    the resolver output is consumed (cat'd) AND its non-zero exit is propagated
    (capture + `|| { ... exit 1; }`), so an invalid GOODFELLOW_PRINCIPLES_WEB
    hard-errors instead of being swallowed by `for f in $(failing-cmd)` (CB1)."""
    for s in CHAIN_SKILLS:
        txt = (SK / s / "SKILL.md").read_text()
        assert 'cat "${CLAUDE_PLUGIN_ROOT}/knowledge/$f"' in txt, (
            f"{s} missing cat consumer"
        )
        # error must be propagated: captured into a var with a failing-exit guard,
        # NOT a bare `for f in $(...)` (which exits 0 on resolver failure)
        assert "principle_files=$(" in txt and "exit 1" in txt, (
            f"{s} does not propagate resolver exit (CB1 regression risk)"
        )
        assert "for f in $(python3" not in txt, (
            f"{s} still uses exit-swallowing for-substitution"
        )


def test_skill_block_actually_hard_errors_on_invalid_env(tmp_path):
    """M2 (behavioral guard, not just substring): extract each skill's real bash
    block and RUN it with an invalid GOODFELLOW_PRINCIPLES_WEB. The block must
    exit non-zero — proving the resolver failure propagates end-to-end, not just
    that the source contains the right strings."""
    # CLAUDE_PLUGIN_ROOT points at the real plugin (has scripts/ + knowledge/principles.md)
    env = {
        k: v for k, v in os.environ.items() if k not in ("GOODFELLOW_PRINCIPLES_WEB",)
    }
    env["CLAUDE_PLUGIN_ROOT"] = str(ROOT)
    env["GOODFELLOW_PRINCIPLES_WEB"] = "true"  # invalid -> resolver exits 1
    for s in CHAIN_SKILLS:
        block = _extract_resolver_block((SK / s / "SKILL.md").read_text())
        assert block, f"{s} has no runnable principles_context bash block"
        # run from a non-web project dir (no package.json) so only the env drives it
        r = subprocess.run(
            ["bash", "-c", block],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
        )
        assert r.returncode != 0, (
            f"{s} block swallowed the resolver failure (exit 0) — CB1 regression"
        )
