"""Skills are found from what people say, not from command names.

0.4.1 removed the `spec-review`, `plan-review` and `grill` aliases. Their
natural-language triggers moved into the canonical skills' descriptions, which
is what the model matches when it picks a skill on its own. These tests pin both
halves: the aliases are gone everywhere, and the phrases they answered to still
reach a skill.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
SK = ROOT / "skills"
REMOVED = ("spec-review", "plan-review", "grill")


def _description(skill: str) -> str:
    text = (SK / skill / "SKILL.md").read_text()
    m = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
    assert m, f"{skill} has no description line"
    return m.group(1).lower()


def test_removed_alias_skills_are_gone():
    for name in REMOVED:
        assert not (SK / name).exists(), f"alias skill {name} still ships"


def test_review_doc_carries_the_aliases_triggers():
    desc = _description("review-doc")
    for phrase in (
        "review my spec",
        "is the spec ready",
        "stress test this spec",
        "review my plan",
        "is the plan ready",
        "stress test this plan",
        "what could go wrong with this plan",
    ):
        assert phrase in desc, f"review-doc description lacks the trigger {phrase!r}"


def test_brainstorm_carries_the_grill_triggers():
    desc = _description("brainstorm")
    for phrase in ("grill me on", "interview me about", "brainstorm", "design"):
        assert phrase in desc, f"brainstorm description lacks the trigger {phrase!r}"


def test_no_live_reference_to_a_removed_alias():
    """Skills, docs and CI must not point anyone at a skill that no longer exists.
    The CHANGELOG keeps its history, and lens_tuning still maps old run-log
    breadcrumbs (`spec-review ...`) recorded before 0.4.1."""
    pattern = re.compile(
        r"goodfellow:(spec-review|plan-review|grill)\b|`(spec-review|plan-review|grill)`"
        r"|skills/(spec-review|plan-review|grill)\b"
    )
    files = [
        *SK.rglob("*.md"),
        ROOT / "README.md",
        ROOT / "CONTRIBUTING.md",
        *(ROOT / "docs").rglob("*.md"),
        *(ROOT / ".github").rglob("*.yml"),
    ]
    hits = [
        f"{p.relative_to(ROOT)}:{n}"
        for p in files
        if p.is_file()
        for n, line in enumerate(p.read_text().splitlines(), 1)
        if pattern.search(line)
    ]
    assert not hits, f"references to removed aliases: {hits}"


def test_ci_does_not_require_the_removed_skills():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    line = next(ln for ln in ci.splitlines() if "for skill in" in ln)
    listed = set(line.split("for skill in", 1)[1].split(";", 1)[0].split())
    assert not listed & set(REMOVED), f"CI still requires {listed & set(REMOVED)}"
