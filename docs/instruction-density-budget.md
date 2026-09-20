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

Principles now load the way Agent Skills already do — a lightweight always-present
menu, with full detail pulled only when relevant:

- **Always injected: the INDEX.** `principles_context.py --index` emits each
  principle's `P-NNN` id, title, and one-line rule (the blockquote). ~2.8k tokens
  for the whole core set — the compressed rule is enough to recognize a relevant or
  violated principle by id.
- **On demand: the body.** Once the model has scanned the index and identified the
  principles that bear on the current task or diff, it pulls their full bodies with
  `principles_context.py --show P-003 P-020`. Requesting a parent id (`P-017`)
  includes its sub-principles (`P-017a`, `P-017b`).
- **Legacy full dump** (`--emit`) is retained for any caller that genuinely wants
  the whole corpus, but the chain skills no longer use it.

Result — what actually loads every run:

| Injected set | Tokens (est) | Entries |
|---|---:|---:|
| core index | ~2,800 | 62 |
| core + web index | ~3,200 | 72 |

An ~83% cut in always-loaded principle tokens, with full detail one command away.

## A. The budget

The **index** is the number that matters: it competes for the model's
instruction-adherence capacity on every run. The full corpus does not — bodies load
on demand.

| Scope | Token cap | Entry cap | Now | Verdict |
|---|---:|---:|---|---|
| core index (default, every user) | **3,000** | **80** | ~2,800 / 62 | within; ~2 principles of headroom |
| core + web index (opt-in) | **3,600** | **95** | ~3,200 / 72 | within; web is chosen extra budget |
| full corpus (bodies) | *advisory* | — | ~16,800 | not always-loaded; WARN only |

The two index caps are enforced by `scripts/test_principle_density.py` (runs in CI).
The full-corpus figure is measured and printed but **not** asserted — it no longer
sits in the always-loaded window. Flipping the full corpus to a hard cap is an
operator call, not a default.

### B. The growth rule (the load-bearing part)

**At or over a cap, growth is displacement, not accumulation.** To add a principle
once the index is at its ceiling, you must merge it into, subsume it under, or delete
another one — not simply append. This is the discipline that keeps "knowledge
compounds" from degrading into "knowledge crowds out." The CI ratchet makes it
checkable: a PR that pushes the index over cap fails until it trades rather than adds.

Corollary: keep one-liners tight. The blockquote *is* the always-loaded rule; the
elaboration belongs in the body, which loads on demand.

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
