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

import pytest

from egress_scan import (
    load_allowlist,
    load_denylist,
    phrase_hits as _phrase_hits,
    scan_text_allowed,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]

# The word-boundary matcher + line-by-line denylist parser are shared with the
# public-PR scrub gate (scripts/egress_scan.py) — ONE mechanism, two callers.
DENY = load_denylist(ROOT / "scripts" / "egress_denylist.txt")


def _principle_text():
    return "\n".join(
        p.read_text() for p in sorted((ROOT / "knowledge").glob("principles*.md"))
    )


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


# ---------------------------------------------------------------------------
# Code / skills / configs egress scan (separate, narrower denylist).
#
# The knowledge denylist above guards knowledge/principles*.md. This second scan
# guards the SHIPPED code surface (scripts/ non-test, skills/, configs/,
# README.md) against upstream-internal leakage, using a denylist that EXCLUDES
# goodfellow's own sanctioned vocabulary (e.g. loop_store) so the port's own
# preserved tooling is not red-gated.
# ---------------------------------------------------------------------------

CODE_DENY = load_denylist(ROOT / "scripts" / "egress_denylist_code.txt")
# Path-scoped exemptions: exact repo-relative file -> denylist phrases it may contain.
CODE_ALLOW = load_allowlist(ROOT / "scripts" / "egress_allowlist_code.txt")


def _code_files():
    files = []
    # scripts/: shipped .py (NOT test_*.py — tests carry deliberate negative-
    # assertion tokens) + .sh
    for p in sorted((ROOT / "scripts").glob("*.py")):
        if not p.name.startswith("test_"):
            files.append(p)
    files.extend(sorted((ROOT / "scripts").glob("*.sh")))
    files.extend(sorted((ROOT / "skills").rglob("*.md")))
    files.extend(sorted((ROOT / "configs").rglob("*")))
    files.extend(sorted((ROOT / "docs").rglob("*.md")))
    readme = ROOT / "README.md"
    if readme.exists():
        files.append(readme)
    # Allowlisted files are always scanned (tests included): an exemption removes one
    # phrase, never the scan.
    files.extend(ROOT / rel for rel in sorted(CODE_ALLOW))
    return sorted({p for p in files if p.is_file()})


def test_no_internal_refs_in_shipped_code():
    leaks = []
    for path in _code_files():
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="ignore")
        for phrase in scan_text_allowed(text, CODE_DENY, CODE_ALLOW.get(rel, frozenset())):
            leaks.append(f"{rel}: {phrase}")
    assert not leaks, f"internal-ref leak in shipped code: {leaks}"


def test_code_denylist_excludes_goodfellow_own_vocab():
    # goodfellow's own tooling nouns must NOT be in the code denylist, or the scan
    # red-gates the port's preserved loop-store references.
    lowered = [d.lower() for d in CODE_DENY]
    assert "loop store" not in lowered
    assert "loop_store" not in lowered


def test_code_scan_catches_a_known_internal_string():
    assert any(_phrase_hits(p, "wired into the matron bridge sink") for p in CODE_DENY)


# ---------------------------------------------------------------------------
# Path-scoped allowlist (the optional remote loop backend).
# ---------------------------------------------------------------------------

# Pinned: widening this list is a reviewed change to this test, never a quiet edit.
PINNED_ALLOW = {
    "scripts/backend_matron.py": frozenset({"matron"}),
    "scripts/test_backend_matron.py": frozenset({"matron"}),
    "docs/using-goodfellow-with-matron.md": frozenset({"matron"}),
}


def test_allowlist_contents_are_pinned():
    assert CODE_ALLOW == PINNED_ALLOW


def test_allowlist_entries_are_existing_exact_files_and_denylisted_phrases():
    deny_lower = {d.lower() for d in CODE_DENY}
    for rel, phrases in CODE_ALLOW.items():
        assert (ROOT / rel).is_file(), f"allowlisted file missing (moved?): {rel}"
        assert phrases <= deny_lower, f"{rel} exempts a phrase that is not denylisted"


def test_only_the_bare_word_is_ever_exempt():
    # The dangerous companions are never exemptable, whatever the file says.
    exempt_anywhere = set().union(*CODE_ALLOW.values())
    for blocked in ("/opt/matron", "matron-bridge", "MATRON_"):
        assert blocked.lower() not in exempt_anywhere
        assert blocked in CODE_DENY


@pytest.mark.parametrize(
    "line",
    [
        "scripts/*.py :: matron",
        "scripts/ :: matron",
        "scripts :: ",
        "/abs/scripts/x.py :: matron",
        "../x.py :: matron",
        "scripts/../x.py :: matron",
        "scripts/x?.py :: matron",
        "scripts/{a,b}.py :: matron",
        "no separator",
    ],
)
def test_allowlist_parser_rejects_globs_and_non_exact_paths(tmp_path, line):
    f = tmp_path / "allow.txt"
    f.write_text(line + "\n")
    with pytest.raises(ValueError):
        load_allowlist(f)


# Blocked tokens are built by concatenation so this test file stays scannable.
_OPT = "/opt/" + "matron"
_BRIDGE = "matron" + "-bridge"
_ENV = "MATRON" + "_TOKEN"


def test_allowed_word_in_an_allowed_path_passes():
    text = "Using Matron: matron://item/1 and GOODFELLOW_MATRON_URL"
    exempt = CODE_ALLOW["scripts/backend_matron.py"]
    assert scan_text_allowed(text, CODE_DENY, exempt) == []


def test_same_word_outside_an_allowed_path_is_caught():
    text = "Using Matron: matron://item/1"
    assert "matron" in scan_text_allowed(text, CODE_DENY, CODE_ALLOW.get("scripts/other.py", frozenset()))


def test_allowed_word_beside_a_blocked_one_is_caught_in_an_allowed_path():
    exempt = CODE_ALLOW["scripts/backend_matron.py"]
    for blocked, phrase in ((_OPT, "/opt/matron"), (_BRIDGE, "matron-bridge"), (_ENV, "MATRON_")):
        hits = scan_text_allowed(f"matron next to {blocked}", CODE_DENY, exempt)
        assert phrase in hits, (blocked, hits)


def test_hosts_and_ips_stay_blocked_in_allowed_paths():
    exempt = CODE_ALLOW["scripts/backend_matron.py"]
    assert "217.216" in scan_text_allowed("matron at 217.216.1.1", CODE_DENY, exempt)
    assert "100.127" in scan_text_allowed("matron at 100.127.0.1", CODE_DENY, exempt)


def test_allowed_files_are_scanned_not_skipped():
    scanned = {p.relative_to(ROOT).as_posix() for p in _code_files()}
    assert set(CODE_ALLOW) <= scanned


def test_prefix_phrases_catch_longer_unprefixed_names_but_not_prefixed_ones():
    prefix = "MATRON" + "_"
    assert _phrase_hits(prefix, "MATRON" + "_TOKEN=x")
    assert not _phrase_hits(prefix, "GOODFELLOW_" + "MATRON_URL")
