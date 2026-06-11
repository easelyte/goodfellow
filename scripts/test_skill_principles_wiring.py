"""Assert all five chain skills wire in the principles read-path (CM1).

A missed skill can't ship silently: every chain skill that reads knowledge must
invoke the principles_context resolver.
"""

import pathlib

SK = pathlib.Path(__file__).resolve().parents[1] / "skills"


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
