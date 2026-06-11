from dedup_principles import match_shipped_principle, parse_shipped

SHIPPED = [
    ("P-002", "Canonical Source of Truth: one authoritative location"),
    ("P-008", "Guard Boundary Inputs"),
]


def test_exact_normalized_match_returns_pid():
    assert (
        match_shipped_principle(
            "  CANONICAL source of truth: one authoritative location ", SHIPPED
        )
        == "P-002"
    )


def test_punctuation_and_ws_normalized():
    assert match_shipped_principle("guard   boundary inputs!!!", SHIPPED) == "P-008"


def test_semantic_but_different_returns_none():
    assert match_shipped_principle("validate webhook payload shape", SHIPPED) is None


def test_empty_returns_none():
    assert match_shipped_principle("", SHIPPED) is None


def test_parse_shipped_extracts_id_and_rule():
    text = (
        "### P-002. Canonical Source of Truth\n"
        "> If you need a sync script to keep two things in agreement, you have one source too many.\n\n"
        "**Anti-pattern:** x\n\n"
        "### P-008. Guard Boundary Inputs\n"
        "> Parse and validate at every trust boundary.\n"
    )
    shipped = parse_shipped(text)
    ids = [s[0] for s in shipped]
    assert ids == ["P-002", "P-008"]
    # rule line is the blockquote text
    assert "sync script" in shipped[0][1]


def test_parse_then_match_roundtrip():
    text = (
        "### P-002. Canonical Source of Truth\n"
        "> If you need a sync script to keep two things in agreement.\n"
    )
    shipped = parse_shipped(text)
    assert (
        match_shipped_principle(
            "if you need a sync script to keep two things in agreement", shipped
        )
        == "P-002"
    )


def test_dedup_fails_closed_on_empty_principles(tmp_path):
    # CM-R3: zero parsed principles (drift/format) must error (exit 1), not pass silently.
    import subprocess, sys, pathlib
    empty = tmp_path / "empty.md"; empty.write_text("# no principle headings here\n")
    r = subprocess.run(
        [sys.executable, "dedup_principles.py", "--description", "x", "--principles", str(empty)],
        cwd=pathlib.Path(__file__).parent, capture_output=True, text=True,
    )
    assert r.returncode == 1 and "fail closed" in r.stderr
