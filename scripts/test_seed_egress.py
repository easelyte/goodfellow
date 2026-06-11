"""Egress guard for the seeded principles.

The principle files ship to a PUBLIC repo. This test is the CI backstop that
fails on any internal product/runtime/customer name leaking into
knowledge/principles*.md. The denylist is a checked-in fixture
(scripts/egress_denylist.txt), one phrase per line, so additions are reviewable.

Matching is WORD-BOUNDARY aware (not raw substring): a denylisted phrase matches
only as a whole word/phrase, not as a substring inside a larger legitimate word.
This is a deliberate deviation from a naive `phrase in text` substring match,
which would false-positive on common vocabulary — e.g. the product name "VIE" is
a substring of "review"/"view"/"viewport", and "store" appears in legitimate
titles like "Authoritative Store Beats Local Mirror" (P-048). Word-boundary
matching catches the real leak ("VIE multi-stage scoring") while leaving the
principle prose ("review", "store", "view") untouched.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]

# PB2: parse LINE-BY-LINE (phrase-preserving), NOT .split() — otherwise
# "loop store" / "mission control" tokenize to "store"/"control" and false-positive
# on legit words (e.g. source P-048 "Authoritative Store Beats Local Mirror").
DENY = [
    l.strip()
    for l in (ROOT / "scripts" / "egress_denylist.txt").read_text().splitlines()
    if l.strip() and not l.startswith("#")
]


def _principle_text():
    return "\n".join(
        p.read_text() for p in sorted((ROOT / "knowledge").glob("principles*.md"))
    )


def _phrase_hits(phrase, text):
    """Case-insensitive denylist match. Word-boundary aware ONLY on edges that are
    word characters: 'VIE' matches 'VIE'/'VIE.' but not 'review'/'viewport'.
    Path-like entries whose edge is a non-word char (e.g. '/root/') get NO boundary
    on that side, so '/root/' correctly matches '/root/workspace' — a word-boundary
    lookahead there would fail on the following 'w' and let the path leak (R4 fix)."""
    left = r"(?<![\w-])" if re.match(r"[\w-]", phrase) else ""
    right = r"(?![\w-])" if re.search(r"[\w-]\Z", phrase) else ""
    return re.search(left + re.escape(phrase) + right, text, re.IGNORECASE) is not None


def test_no_denylisted_phrases_in_seed():
    text = _principle_text()
    hits = [phrase for phrase in DENY if _phrase_hits(phrase, text)]
    assert not hits, f"egress leak in principles*.md: {hits}"


def test_easelyte_is_allowed_publisher():
    assert "easelyte" not in [d.lower() for d in DENY]  # publisher name, allowed


def test_known_internal_string_would_be_caught():
    sample = "this mentions VIE multi-stage scoring"
    assert any(_phrase_hits(phrase, sample) for phrase in DENY)


def test_legit_word_store_not_false_positive():
    # P-048 "Authoritative Store Beats Local Mirror" must NOT trip the guard
    assert not any(
        _phrase_hits(phrase, "authoritative store beats local mirror")
        for phrase in DENY
    )


def test_legit_word_review_not_false_positive():
    # 'review'/'view'/'viewport' must NOT trip on the 'VIE' product token
    assert not any(
        _phrase_hits(phrase, "adversarial review of the viewport view")
        for phrase in DENY
    )


def test_root_path_prefix_is_caught(tmp_path):
    # R4 fix: a path-prefix denylist entry ('/root/') must catch '/root/workspace...'
    # even though a word char follows the trailing slash.
    assert any(
        _phrase_hits(phrase, "leak: /root/workspace/goodfellow/x") for phrase in DENY
    )
