"""Structural tests for the seeded principle files (T-1.5)."""

import re
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
HEADER = (
    "Goodfellow seed principles — curated from easelyte's cross-repo design knowledge."
)


def test_every_entry_has_unique_pid():
    text = (ROOT / "knowledge" / "principles.md").read_text()
    ids = re.findall(r"^### (P-\d{3})\.", text, re.M)
    assert ids and len(ids) == len(set(ids)), "missing or duplicate P-NNN ids"


def test_ids_are_three_digit():
    text = "\n".join(p.read_text() for p in (ROOT / "knowledge").glob("principles*.md"))
    assert not re.search(r"### P-\d{1,2}\.", text), "found non-3-digit P-id"


def test_sanitized_header_present():
    assert HEADER in (ROOT / "knowledge" / "principles.md").read_text()


def test_web_header_present():
    assert HEADER in (ROOT / "knowledge" / "principles-web.md").read_text()


def test_ids_unique_across_both_files():
    text = "\n".join(
        p.read_text() for p in sorted((ROOT / "knowledge").glob("principles*.md"))
    )
    ids = re.findall(r"^### (P-\d{3})\.", text, re.M)
    assert len(ids) == len(set(ids)), "duplicate P-NNN id across core/web files"
