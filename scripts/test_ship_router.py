"""Guard the ship skill's follow-up-loop router against the silent MAJOR drop.

The reviewers emit a three-tier severity taxonomy (`## Blockers` / `## Major` /
`## Minor`; ranks blocker > major > minor, per convergence_detector.SEVERITY_RANKS).
The ship router in section "5. File follow-up loops" must give EVERY substantive tier
a durable destination. The historical bug: the router only handled two tiers
(safety-critical -> loops, polish-tier -> gotchas), so a MAJOR finding (rank 2, between
them) matched neither branch and was silently dropped.

The router now carries a machine-readable canonical routing map. These tests parse THAT
map exactly (not fuzzy prose substrings, which a reversed clause like "do not file to the
loop store" would slip past) and assert the three-tier contract:
blocker + major -> loop store; minor -> knowledge gotchas; no tier left unrouted.
A dedicated mutation-fixture set proves the parser rejects a re-introduced major drop.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
SHIP_SKILL = ROOT / "skills" / "ship" / "SKILL.md"

# The three substantive severity tiers and their required durable destinations.
EXPECTED_ROUTING = {
    "blocker": "loop_store",
    "major": "loop_store",
    "minor": "knowledge_gotchas",
}
VALID_DESTINATIONS = {"loop_store", "knowledge_gotchas"}

_ROUTE_LINE = re.compile(
    r"^\s*(blocker|major|minor)\s*->\s*(loop_store|knowledge_gotchas)\b",
    re.M,
)


def _follow_up_section(text):
    """Return the body of the '## 5. File follow-up loops' section (up to next ## header)."""
    m = re.search(
        r"^##\s*5\.\s*File follow-up loops\s*$(.*?)(?=^##\s|\Z)",
        text,
        re.S | re.M,
    )
    assert m, "ship SKILL.md has no '## 5. File follow-up loops' section"
    return m.group(1)


def parse_routing_map(section_text):
    """Extract the machine-readable {severity: destination} routing map.

    Only accepts exact `<tier> -> <destination>` lines, so a negated or contradictory
    prose clause cannot masquerade as a routing decision. Returns a dict; a tier listed
    more than once with conflicting destinations raises (ambiguous routing is a defect).
    """
    routing = {}
    for tier, dest in _ROUTE_LINE.findall(section_text):
        if tier in routing and routing[tier] != dest:
            raise ValueError(
                f"tier {tier!r} routed to both {routing[tier]!r} and {dest!r}"
            )
        routing[tier] = dest
    return routing


def validate_routing(section_text):
    """Return a list of contract violations; empty list means the router is sound.

    Enforces: every substantive tier present, each mapped to a valid destination,
    blocker+major -> loop_store, minor -> knowledge_gotchas. This is the single predicate
    the mutation fixtures exercise, so a re-introduced drop or a reversed route is caught.
    """
    errors = []
    routing = parse_routing_map(section_text)
    for tier, want in EXPECTED_ROUTING.items():
        got = routing.get(tier)
        if got is None:
            errors.append(f"{tier} tier has no routing destination (dropped)")
        elif got not in VALID_DESTINATIONS:
            errors.append(f"{tier} routed to invalid destination {got!r}")
        elif got != want:
            errors.append(f"{tier} routed to {got!r}, expected {want!r}")
    return errors


# --- Contract tests against the real SKILL.md ---------------------------------


def test_router_declares_canonical_routing_map():
    section = _follow_up_section(SHIP_SKILL.read_text())
    routing = parse_routing_map(section)
    assert routing == EXPECTED_ROUTING, (
        f"ship router routing map is {routing}, expected {EXPECTED_ROUTING}"
    )


def test_major_findings_route_to_the_loop_store():
    """Core drop-bug guard: MAJOR must map to the loop store, never gotchas, never absent."""
    section = _follow_up_section(SHIP_SKILL.read_text())
    routing = parse_routing_map(section)
    assert routing.get("major") == "loop_store", (
        "MAJOR findings must be durably filed to the loop store — not dropped, "
        "not downgraded to a knowledge gotcha"
    )


def test_no_substantive_tier_left_unrouted():
    section = _follow_up_section(SHIP_SKILL.read_text())
    assert validate_routing(section) == [], (
        "a substantive severity tier is unrouted or mis-routed: "
        f"{validate_routing(section)}"
    )


def test_prose_names_all_three_tiers():
    """Prose must still name all three tiers so the map is human-legible, not just parseable."""
    section = _follow_up_section(SHIP_SKILL.read_text()).lower()
    assert "blocker" in section or "safety-critical" in section
    assert "major" in section, "router prose does not mention the MAJOR tier"
    assert "minor" in section or "polish" in section


# --- Mutation / negative fixtures: prove the validator catches a re-introduced drop ---


def _synthetic_section(map_lines):
    return (
        "\n\nsome prose about deferred findings\n\n```text\n"
        + "\n".join(map_lines)
        + "\n```\n"
    )


def test_validator_rejects_major_downgraded_to_gotcha():
    bad = _synthetic_section(
        [
            "blocker -> loop_store",
            "major   -> knowledge_gotchas",
            "minor   -> knowledge_gotchas",
        ]
    )
    errs = validate_routing(bad)
    assert any("major" in e for e in errs), (
        "validator failed to flag a major finding routed to gotchas (the drop bug)"
    )


def test_validator_rejects_missing_major_tier():
    bad = _synthetic_section(["blocker -> loop_store", "minor   -> knowledge_gotchas"])
    errs = validate_routing(bad)
    assert any("major" in e and "dropped" in e for e in errs), (
        "validator failed to flag a wholly-absent major tier"
    )


def test_validator_rejects_blocker_downgraded():
    bad = _synthetic_section(
        [
            "blocker -> knowledge_gotchas",
            "major   -> loop_store",
            "minor   -> knowledge_gotchas",
        ]
    )
    assert any("blocker" in e for e in validate_routing(bad))


def test_validator_accepts_correct_map():
    good = _synthetic_section(
        [
            "blocker -> loop_store",
            "major   -> loop_store",
            "minor   -> knowledge_gotchas",
        ]
    )
    assert validate_routing(good) == []


def test_reversed_prose_clause_does_not_fool_the_parser():
    """A line that merely *mentions* 'loop_store' inside a negation must NOT be read as a
    route. Only the exact `tier -> dest` form counts, so the prose 'never file major to
    loop_store' does not create a routing entry."""
    section = (
        "\n\n- major findings are important, never send them nowhere\n"
        "prose: do not file to loop_store as a gotcha\n\n```text\n"
        "blocker -> loop_store\nminor   -> knowledge_gotchas\n```\n"
    )
    # major has no *exact* route line, so it must be reported as dropped.
    assert any("major" in e and "dropped" in e for e in validate_routing(section))
