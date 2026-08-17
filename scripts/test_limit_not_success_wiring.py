"""Source-text wiring tests for P-079 "Reaching a Limit Is Not Success".

P-079 is implemented in the chain-skill markdown (the runtime for goodfellow's
review/execute loops), not in a unit-testable code path. Per canonical principle
#71 (wiring in a non-testable entry point needs a source-text wiring test), these
assertions pin the load-bearing prose so a future edit cannot silently drop the
terminal-halt behavior or reintroduce the honesty gap.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
SK = ROOT / "skills"
PRINCIPLES = ROOT / "knowledge" / "principles.md"


def _read(*parts):
    return (ROOT.joinpath(*parts)).read_text()


def test_principle_p079_defined_with_canonical_title():
    text = PRINCIPLES.read_text()
    assert "### P-079. Reaching a Limit Is Not Success" in text, (
        "P-079 principle missing or retitled"
    )


def test_p079_does_not_collide_with_canonical_71():
    """The canonical registry assigns #71 to the source-text wiring principle;
    P-071 must NOT be reused for the limit-not-success principle (would break the
    P-NNN <-> canonical alignment contract)."""
    text = PRINCIPLES.read_text()
    assert "P-071. Reaching a Limit" not in text
    m = re.search(r"^### (P-\d{3})\. Reaching a Limit Is Not Success", text, re.M)
    assert m and m.group(1) == "P-079", "limit-not-success principle must be P-079"


def test_execute_ships_only_on_success_path():
    """execute must not auto-dispatch ship after a stopped/limit/failed run."""
    txt = _read("skills", "execute", "SKILL.md")
    assert "P-079" in txt
    assert "Terminal gate before shipping" in txt
    # The whole summary is conditional: a partial run says "halted", never "complete".
    assert "Execution halted" in txt
    # Ship dispatch is gated on the success path, not unconditional.
    assert "Dispatch ship ONLY on the success path" in txt


def test_spec_review_has_terminal_safety_gate():
    txt = _read("skills", "spec-review", "SKILL.md")
    assert "P-079" in txt
    assert "Terminal safety gate" in txt
    assert "spec_review_halt" in txt
    # A safety-critical cap-halt must not cascade to plan (normalize wraps).
    flat = " ".join(txt.split())
    assert "do NOT discard the findings and do NOT auto-dispatch plan" in flat


def test_spec_review_cap_is_limit_not_convergence():
    """§5 must classify a non-blocking hard-cap halt as a limit, never as
    convergence — otherwise the model can emit an unverified convergence claim
    (the exact P-079 anti-pattern). Negative assertion pins the fix."""
    txt = _read("skills", "spec-review", "SKILL.md")
    flat = " ".join(txt.split())
    assert "Only non-blocking findings → declare convergence" not in flat
    assert "resolved | limit_reached" in flat


def test_plan_review_has_terminal_safety_gate():
    txt = _read("skills", "plan-review", "SKILL.md")
    assert "P-079" in txt
    assert "Terminal safety gate" in txt
    assert "plan_review_halt" in txt
    # A safety-critical cap-halt must not cascade to execute (normalize wraps).
    flat = " ".join(txt.split())
    assert "do NOT discard the findings and do NOT auto-dispatch execute" in flat


def test_plan_review_discard_is_conditional():
    """The discard instruction must be scoped to the non-blocking path so it does
    not contradict the terminal safety gate (which preserves findings)."""
    txt = _read("skills", "plan-review", "SKILL.md")
    assert "On a safety-critical cap-halt, do NOT discard" in txt


def test_plan_review_cap_is_limit_not_convergence():
    """§5 must classify a non-blocking hard-cap halt as a limit, never as
    convergence (P-079). Mirror of the spec-review assertion (P-057 parity)."""
    txt = _read("skills", "plan-review", "SKILL.md")
    flat = " ".join(txt.split())
    assert "Only non-blocking findings → declare convergence" not in flat
    assert "resolved | limit_reached" in flat


def test_ship_reports_cap_halt_honestly():
    txt = _read("skills", "ship", "SKILL.md")
    assert "P-079" in txt
    assert "Halted at hard cap" in txt
