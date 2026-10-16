"""Natural-language triggers route to canonical skills."""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
SK = ROOT / "skills"
REMOVED = (
    "spec-review",
    "plan-review",
    "review-doc",
    "codex-review",
    "public-pr",
    "prune-stale",
    "snap-compact",
)


def _description(skill: str) -> str:
    text = (SK / skill / "SKILL.md").read_text()
    m = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
    assert m, f"{skill} has no description line"
    return m.group(1).lower()


def test_removed_alias_skills_are_gone():
    for name in REMOVED:
        assert not (SK / name).exists(), f"alias skill {name} still ships"


def test_review_carries_the_document_triggers():
    desc = _description("review")
    for phrase in (
        "review my spec",
        "is the spec ready",
        "stress test this spec",
        "review my plan",
        "is the plan ready",
        "stress test this plan",
        "what could go wrong with this plan",
    ):
        assert phrase in desc, f"review description lacks the trigger {phrase!r}"


def test_brainstorm_keeps_its_design_triggers():
    desc = _description("brainstorm")
    for phrase in ("brainstorm", "design"):
        assert phrase in desc, f"brainstorm description lacks the trigger {phrase!r}"


def test_grill_carries_the_interview_triggers():
    desc = _description("grill")
    for phrase in ("grill me on", "interview me about"):
        assert phrase in desc, f"grill description lacks the trigger {phrase!r}"


def test_no_live_reference_to_a_removed_alias():
    """Skills, docs and CI must not point anyone at a skill that no longer exists.
    The CHANGELOG keeps its history, and lens_tuning still maps old run-log
    breadcrumbs (`spec-review ...`) recorded before 0.4.1."""
    pattern = re.compile(
        r"goodfellow:(spec-review|plan-review)\b|`(spec-review|plan-review)`"
        r"|skills/(spec-review|plan-review)\b"
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


def test_core_detail_pointers_exist():
    for core in SK.glob("*/SKILL.md"):
        for target in re.findall(r"`([^`]+\.md)`", core.read_text()):
            if "/" not in target or target.startswith("../"):
                assert (core.parent / target).is_file(), (
                    f"{core}: missing detail {target}"
                )


def test_grill_routes_before_interview():
    core = (SK / "grill/SKILL.md").read_text()
    assert "../brainstorm/routing.md" in core
    for required in ("--tier", "--from-loop", "T0/T1", "would_route"):
        assert required in core
    assert core.index("../brainstorm/routing.md") < core.index("## Scout first")
