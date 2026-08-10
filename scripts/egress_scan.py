#!/usr/bin/env python3
"""Shared egress / internal-ref scrub matcher.

ONE implementation of the word-boundary-aware denylist match, reused by:
  * the CI backstop over knowledge/ + scripts/ + skills/ (test_seed_egress.py), and
  * the public-PR scrub gate (public_pr_scrub.py).

Matching is WORD-BOUNDARY aware (not raw substring): a denylisted phrase matches
only as a whole word/phrase, so a 3-letter token like "CAT" matches "CAT" but not
"category"/"catalog". Path-like entries whose edge is a non-word char (e.g. a
'/abs/path/' prefix) get NO boundary on that side, so the prefix still matches a
longer path that continues with a word character.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Sequence, Tuple


def load_denylist(path: Path) -> List[str]:
    """Parse a denylist file: one phrase per line, `#` comments, blanks ignored.

    Parse LINE-BY-LINE (phrase-preserving), NOT by whitespace-splitting — a
    multi-word phrase like "acme corp" must stay one entry, or it tokenizes to
    "corp" and false-positives on ordinary prose.
    """
    return [
        line.strip()
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def phrase_hits(phrase: str, text: str) -> bool:
    """Case-insensitive denylist match, word-boundary aware only on word-char edges.

    'CAT' matches 'CAT'/'CAT.' but not 'category'/'catalog'. A path-prefix entry
    whose edge is a non-word char (e.g. an '/abs/path/' prefix) gets NO boundary
    there, so it still matches a longer path that continues with a word char.
    """
    left = r"(?<![\w-])" if re.match(r"[\w-]", phrase) else ""
    right = r"(?![\w-])" if re.search(r"[\w-]\Z", phrase) else ""
    return re.search(left + re.escape(phrase) + right, text, re.IGNORECASE) is not None


def scan_text(text: str, denylist: Sequence[str]) -> List[str]:
    """Return the denylist phrases that hit anywhere in `text`."""
    return [p for p in denylist if phrase_hits(p, text)]


def scan_paths(
    paths: Sequence[Path], denylist: Sequence[str]
) -> List[Tuple[Path, str]]:
    """Return (path, phrase) for every denylist hit across the given files."""
    hits: List[Tuple[Path, str]] = []
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for phrase in denylist:
            if phrase_hits(phrase, text):
                hits.append((Path(path), phrase))
    return hits
