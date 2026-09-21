"""Ratchet: the always-injected principle INDEX stays under its budget.

This is the "growth is displacement, not accumulation" gate. The full principle
corpus may grow (bodies load on demand via `--show`), but the INDEX injected on
every chain run must stay under the research-derived cap. When a new principle
pushes the index over, CI fails here — forcing the author to merge/subsume rather
than accumulate (docs/instruction-density-budget.md §B).

The full-corpus size is deliberately NOT asserted (advisory WARN only): it no
longer sits in the always-loaded window. Flipping the full corpus to a hard cap is
an operator call, documented in the budget doc.
"""

import os
import pathlib

import measure_principle_density as m
import principles_context as pc
import pytest

PLUGIN_ROOT = str(pathlib.Path(__file__).resolve().parents[1])


def _measure(force_web):
    entries, index, _full, web_active = m.gather(
        plugin_root=PLUGIN_ROOT, project_root=PLUGIN_ROOT, force_web=force_web
    )
    _, tok, _ = m.measure(index)
    # Tier-1 rows (vital-few + category routing rows) are what the always-loaded
    # budget caps — not the total corpus, which grows on demand.
    return tok, pc.index_entry_count(entries), web_active


def test_core_index_within_cap():
    tok, n, _ = _measure(force_web=False)
    assert tok <= m.INDEX_TOKEN_CAP, (
        f"core principle index {tok} tok > cap {m.INDEX_TOKEN_CAP}; "
        "displace/merge a principle instead of adding one"
    )
    assert n <= m.INDEX_ENTRY_CAP, f"core index {n} entries > cap {m.INDEX_ENTRY_CAP}"


def test_core_plus_web_index_within_cap():
    tok, n, web_active = _measure(force_web=True)
    assert web_active, "forcing web should activate the web supplement"
    assert tok <= m.INDEX_TOKEN_CAP_WEB, (
        f"core+web principle index {tok} tok > cap {m.INDEX_TOKEN_CAP_WEB}; "
        "displace/merge a principle instead of adding one"
    )
    assert n <= m.INDEX_ENTRY_CAP_WEB, (
        f"core+web index {n} entries > cap {m.INDEX_ENTRY_CAP_WEB}"
    )


def test_env_web_opt_in_selects_web_cap_without_flag(monkeypatch):
    """Regression: GOODFELLOW_PRINCIPLES_WEB=1 (env, no --web flag) must load web
    AND be measured against the web cap — cap selection follows the RESOLVED corpus,
    not just the CLI flag. Otherwise a valid core+web corpus false-fails the ratchet."""
    monkeypatch.setenv("GOODFELLOW_PRINCIPLES_WEB", "1")
    rc = m.main(
        ["--plugin-root", PLUGIN_ROOT, "--project-root", PLUGIN_ROOT, "--check"]
    )
    assert rc == 0, "env web opt-in was measured against the wrong (core) cap"


def test_gather_does_not_persist_env_mutation():
    """--web forces web for the measurement only; it must not leak into os.environ."""
    os.environ.pop("GOODFELLOW_PRINCIPLES_WEB", None)
    m.gather(plugin_root=PLUGIN_ROOT, project_root=PLUGIN_ROOT, force_web=True)
    assert "GOODFELLOW_PRINCIPLES_WEB" not in os.environ


def test_index_is_far_smaller_than_full_corpus():
    """Progressive disclosure invariant: the always-injected index must be a small
    fraction of the full corpus — otherwise the disclosure bought nothing."""
    entries = pc.load_entries(plugin_root=PLUGIN_ROOT, project_root=PLUGIN_ROOT)
    _, idx_tok, _ = m.measure(pc.build_index(entries))
    _, full_tok, _ = m.measure(
        pc.emit_principles(plugin_root=PLUGIN_ROOT, project_root=PLUGIN_ROOT)
    )
    assert idx_tok < full_tok * 0.35, (
        f"index {idx_tok} tok is not meaningfully smaller than full {full_tok} tok"
    )


def test_no_malformed_or_duplicate_principle_headers():
    """The shipped corpus must fully parse: every principle-looking header matches
    the canonical grammar and every id is unique. Guards against a corpus edit that
    would silently vanish from injection and evade the growth ratchet."""
    # load_entries raises ConfigError on a malformed header or a duplicate id.
    entries = pc.load_entries(plugin_root=PLUGIN_ROOT, project_root=PLUGIN_ROOT)
    assert entries, "shipped corpus parsed to zero principles"


def test_malformed_header_fails_loud(tmp_path):
    """A principle-looking header that drifts from `P-NNN. Title` must hard-error,
    not silently disappear."""
    with pytest.raises(pc.ConfigError):
        pc.parse_principles("### P-080 — Drifted Header\n> rule.\n", source="bad.md")


# --- tiered index + category routing (feat: category routing) ---

def _parse(text):
    return pc.parse_principles(text, source="t.md")


def test_cat_marker_assigns_category():
    [e] = _parse("### P-900. X\n> rule.\n<!-- cat: testing -->\n\nbody\n")
    assert e["cat"] == "testing"


def test_untagged_routes_to_general():
    [e] = _parse("### P-900. X\n> rule.\n\nbody, no marker\n")
    assert e["cat"] == "general"


def test_cat_scoped_to_own_segment_not_child():
    """F2 regression: an untagged PARENT with a tagged sub-principle must route to
    `general` — the child's marker must not leak onto the parent, even though the
    parent's body includes the child."""
    text = (
        "### P-900. Parent\n> parent rule.\n\nparent body\n\n"
        "#### P-900a. Child\n> child rule.\n<!-- cat: security -->\n\nchild body\n"
    )
    entries = {e["id"]: e for e in _parse(text)}
    assert entries["P-900"]["cat"] == "general", "child category leaked to parent"
    assert entries["P-900a"]["cat"] == "security"


def test_tagged_parent_keeps_own_category_over_child():
    text = (
        "### P-900. Parent\n> parent rule.\n<!-- cat: correctness -->\n\nbody\n\n"
        "#### P-900a. Child\n> child rule.\n<!-- cat: security -->\n\nchild\n"
    )
    entries = {e["id"]: e for e in _parse(text)}
    assert entries["P-900"]["cat"] == "correctness"
    assert entries["P-900a"]["cat"] == "security"


def test_tiered_index_omits_nonvital_oneliners():
    """The tiered --index shows vital-few one-liners + a category table (ids only);
    a non-vital principle's one-liner must NOT appear inline (that is the whole
    point of the budget-flat index)."""
    entries = pc.load_entries(plugin_root=PLUGIN_ROOT, project_root=PLUGIN_ROOT)
    nonvital = next(
        e for e in entries if e["id"] not in pc.VITAL_FEW and e["oneliner"]
    )
    idx = pc.build_index(entries)
    assert nonvital["id"] in idx, "id should be in a category row"
    assert nonvital["oneliner"] not in idx, "non-vital one-liner must not be inline"


def test_index_flat_includes_all_oneliners():
    """F1 regression: the flat index (used by the review bridge) must carry EVERY
    principle's one-liner inline, including non-vital ones, so a one-shot reviewer
    prompt can check them without expanding a category."""
    entries = pc.load_entries(plugin_root=PLUGIN_ROOT, project_root=PLUGIN_ROOT)
    flat = pc.build_index_flat(entries)
    nonvital = next(
        e for e in entries if e["id"] not in pc.VITAL_FEW and e["oneliner"]
    )
    assert nonvital["oneliner"] in flat, "flat index dropped a non-vital one-liner"


def test_show_category_unknown_is_visible():
    entries = pc.load_entries(plugin_root=PLUGIN_ROOT, project_root=PLUGIN_ROOT)
    out = pc.show_category(entries, "nonexistent-cat")
    assert "no principles" in out
