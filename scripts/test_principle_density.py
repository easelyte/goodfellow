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

PLUGIN_ROOT = str(pathlib.Path(__file__).resolve().parents[1])


def _index_measure(web):
    env = dict(os.environ)
    env.pop("GOODFELLOW_PRINCIPLES_WEB", None)
    if web:
        env["GOODFELLOW_PRINCIPLES_WEB"] = "1"
    # gather() mutates os.environ for web; isolate via a saved/restore
    saved = os.environ.get("GOODFELLOW_PRINCIPLES_WEB")
    try:
        if web:
            os.environ["GOODFELLOW_PRINCIPLES_WEB"] = "1"
        else:
            os.environ.pop("GOODFELLOW_PRINCIPLES_WEB", None)
        entries = pc.load_entries(plugin_root=PLUGIN_ROOT, project_root=PLUGIN_ROOT)
        index = pc.build_index(entries)
    finally:
        if saved is None:
            os.environ.pop("GOODFELLOW_PRINCIPLES_WEB", None)
        else:
            os.environ["GOODFELLOW_PRINCIPLES_WEB"] = saved
    _, tok, _ = m.measure(index)
    return tok, len(entries)


def test_core_index_within_cap():
    tok, n = _index_measure(web=False)
    assert tok <= m.INDEX_TOKEN_CAP, (
        f"core principle index {tok} tok > cap {m.INDEX_TOKEN_CAP}; "
        "displace/merge a principle instead of adding one"
    )
    assert n <= m.INDEX_ENTRY_CAP, f"core index {n} entries > cap {m.INDEX_ENTRY_CAP}"


def test_core_plus_web_index_within_cap():
    tok, n = _index_measure(web=True)
    assert tok <= m.INDEX_TOKEN_CAP_WEB, (
        f"core+web principle index {tok} tok > cap {m.INDEX_TOKEN_CAP_WEB}; "
        "displace/merge a principle instead of adding one"
    )
    assert n <= m.INDEX_ENTRY_CAP_WEB, (
        f"core+web index {n} entries > cap {m.INDEX_ENTRY_CAP_WEB}"
    )


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
