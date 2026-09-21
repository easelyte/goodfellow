# Instruction density: the principle-injection budget

A measured budget for what Goodfellow injects into a chain run's context, so the
seeded principle corpus can grow without silently eroding the model's ability to
follow any of it. Recompute anytime with:

```bash
python3 scripts/measure_principle_density.py          # table + verdict
python3 scripts/measure_principle_density.py --web     # include principles-web.md
python3 scripts/measure_principle_density.py --json     # machine-readable
```

## Why this exists

"Knowledge that compounds" is one of Goodfellow's headline promises — and it is a
real good, up to a point. Past a ceiling, adding rules stops helping and starts
hurting, because a model's capacity to *simultaneously honor* a set of instructions
is finite. Three findings from the instruction-following / long-context literature:

- **Accuracy erodes past roughly 2,500–3,000 system-prompt tokens.** Beyond that,
  primacy/recency bias starves rules in the middle of the context — they are present
  but not reliably applied.
- **Reasoning models hold near-perfect adherence up to a density cliff at ~150–250
  instructions**, then drop sharply with rising variance.
- **All-instructions-satisfied probability decays roughly exponentially in
  instruction count.** Each added rule slightly degrades adherence to every other
  rule — a rule is not free, even when the rule is good.

Goodfellow shipped ~74 seeded principles across `knowledge/principles.md` (core) and
`knowledge/principles-web.md` (opt-in). Injecting the **full bodies** on every chain
run cost roughly:

| Injected set | Tokens (est) | Directives (est) |
|---|---:|---:|
| core full bodies | ~16,800 | ~296 |
| core + web full bodies | ~19,900 | ~349 |

That is ~6× the erosion ceiling and well past the adherence cliff — on *every*
brainstorm, spec-review, plan, plan-review, and execute run, before the skill's own
instructions and the task itself are even added. The principles were, in effect,
crowding out the work they were meant to guide.

> **Estimation caveat.** Token counts are `chars/4` (labeled estimates; no local
> tokenizer ships with the plugin, and a metered token-count API call is out of
> scope). Treat token figures as ±15% and directive counts as ±30%. They are
> applied identically every run, so *deltas* over time are what matter.

## The fix: progressive disclosure

Principles now load the way Agent Skills — and this box's own memory system — already
do: a lightweight always-present menu that routes by topic, with detail pulled only
when relevant. **Three tiers**, so the corpus can grow without inflating what loads
every run:

- **Tier 1 — always injected: the tiered INDEX.** `principles_context.py --index`
  emits the *vital-few* full one-liners (see §C) **plus a category routing table** —
  one row per category (`security`, `data-integrity`, `correctness`, `testing`,
  `review-process`, `reliability`, `integration`, `agent-runtime`, `ui`) listing its
  member `P-NNN` ids, *without* each principle's one-liner. Adding a principle adds
  one id to a category row (~2 tokens), not a ~45-token one-liner — so tier 1 stays
  flat as the corpus grows. This is the same shape as this repo's `MEMORY.md`
  (vital-few + a domain routing table); category membership is set per principle by
  an inline `<!-- cat: NAME -->` marker.
- **Tier 2 — on demand: a category's one-liners.** Seeing a category relevant to the
  task, the model expands it with `principles_context.py --category testing` to get
  the `P-NNN` + title + one-liner for every principle in it.
- **Tier 3 — on demand: the body.** It then pulls the full bodies that bear on the
  work with `principles_context.py --show P-003 P-020`. Requesting a parent id
  (`P-017`) includes its sub-principles (`P-017a`, `P-017b`).
- **Legacy full dump** (`--emit`) is retained for any caller that genuinely wants
  the whole corpus, but the chain skills no longer use it.

Result — what actually loads every run (tier 1 only):

| Injected set | Tokens (est) | Tier-1 rows |
|---|---:|---:|
| core index | ~720 | 16 (8 vital + 8 categories) |
| core + web index | ~800 | 17 |

An ~96% cut from the pre-disclosure full-corpus injection, and flat under corpus
growth — a new principle costs one id in a category row, full detail two commands away.

## A. The budget

The **tier-1 index** is the number that matters: it competes for the model's
instruction-adherence capacity on every run. The full corpus does not — bodies load
on demand. The caps are measured against tier 1 (vital-few one-liners + the category
routing table), and the **entry cap counts tier-1 rows** (`index_entry_count` =
vital-few present + distinct categories), *not* the total corpus — so the corpus can
grow indefinitely while tier 1 stays flat.

| Scope | Token cap | Tier-1 row cap | Now | Verdict |
|---|---:|---:|---|---|
| core index (default, every user) | **3,000** | **80** | ~720 / 16 | within; large headroom under growth |
| core + web index (opt-in) | **3,600** | **95** | ~800 / 17 | within; web is chosen extra budget |
| full corpus (bodies) | *advisory* | — | ~20,500 / 75 entries | not always-loaded; WARN only |

The two tier-1 caps are enforced by `scripts/test_principle_density.py` (runs in CI).
The full-corpus figure is measured and printed but **not** asserted — it no longer
sits in the always-loaded window. Flipping the full corpus to a hard cap is an
operator call, not a default.

### B. The growth rule (the load-bearing part)

**Adding an ordinary principle is now cheap — it routes to a category.** Under the
tiered index, a new principle adds its body to the corpus and its id to a category
row; tier 1 grows by ~2 tokens, not a one-liner. So the corpus can compound without
crowding out the always-loaded window. Tag it with `<!-- cat: NAME -->` and it lands
in the right routing row.

**Displacement now applies only to tier 1.** The ceiling still bites for the two
things that *do* load every run: the `VITAL_FEW` set and the category list. Promoting
a principle into `VITAL_FEW`, or adding a whole new category, must trade against the
tier-1 budget — merge, subsume, or drop rather than append. The CI ratchet enforces
this: a PR that pushes tier 1 over cap (too many vital-few one-liners, or category
proliferation) fails until it trades.

Corollary: keep one-liners tight and don't over-split categories. The blockquote *is*
the tier-2 rule and a vital-few one-liner *is* always-loaded; elaboration belongs in
the body, which loads on demand. A handful of broad categories routes better than
dozens of narrow ones.

## C. Placement

Token counts govern *how much*; placement governs *which rules survive* the
mid-context adherence dip.

- **Vital-few at the edges.** The highest-consequence, expensive-to-reverse
  principles — authorization, canonical source, fail-visible, data egress,
  irreversibility, idempotency, diverged-base merges, "a limit reached is not
  success" — lead the index (primacy) and are recapped on its last line (recency).
  The set lives in `VITAL_FEW` in `principles_context.py`; adjust it there.
- **Never bury a safety rule in the middle.** The middle third of a long context is
  where adherence dies. If a principle protects against data loss or a privilege
  boundary, its one-liner belongs at an edge, not the interior.

## Re-checking

Run `scripts/measure_principle_density.py` after any material change to the seeded
principles and confirm the caps in §A still hold. The caps and research thresholds
are pinned as constants at the top of that script.
