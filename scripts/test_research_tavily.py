"""Tests for research_tavily.py output labeling honesty (loop #389)."""

import research_tavily


def test_markdown_does_not_claim_verification():
    results = [
        {
            "claim": "Foo supports bar",
            "status": "✓",
            "detail": "snippet",
            "url": "http://x",
        },
        {"claim": "Baz exists", "status": "?", "detail": "Low relevance", "url": ""},
    ]
    md = research_tavily.format_markdown(results)
    # The header must not overclaim "Verified".
    assert "## Verified Claims" not in md
    assert "Researched Claims" in md
    # A caveat must make clear ✓ is relevance, not a verdict.
    assert "NOT a verification verdict" in md
    assert "cannot detect refutation" in md


def test_markdown_keeps_status_glyphs():
    results = [
        {"claim": "c1", "status": "✓", "detail": "d", "url": "http://x"},
        {"claim": "c2", "status": "?", "detail": "d", "url": ""},
    ]
    md = research_tavily.format_markdown(results)
    assert "✓ **c1**" in md
    assert "? **c2**" in md
