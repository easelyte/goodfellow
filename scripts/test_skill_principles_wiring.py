"""Assert all five chain skills wire in the principles read-path (CM1).

A missed skill can't ship silently: every chain skill that reads knowledge must
invoke the principles_context resolver.
"""

import pathlib

SK = pathlib.Path(__file__).resolve().parents[1] / "skills"


def test_all_five_skills_invoke_principles_context():
    for s in ("brainstorm", "spec-review", "plan", "plan-review", "execute"):
        txt = (SK / s / "SKILL.md").read_text()
        assert "principles_context.py" in txt, f"{s} missing principles read-path"
