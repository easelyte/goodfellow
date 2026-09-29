---
name: brainstorm
description: Entry point for new work. Classifies the change into a risk tier (T0 fix … T3 live state), then does only the design work that tier needs — none for a fix, a short plan for a feature, a reviewed spec for a design. Reads accumulated principles first. `--grill` interviews you one question at a time for fuzzy intent; `--from-loop N` seeds from a tracked follow-up; `--tier Tn` overrides the tier (never below its hard floor).
---

Start the work described in: $ARGUMENTS

Flags: `--grill` (interview mode, §5b), `--from-loop N` (§3), `--tier T0..T3` (operator override,
§1). Autopilot is on by default (`GOODFELLOW_AUTOPILOT=0` turns it off): no approval gates between
steps. It still stops for **product calls** (naming, pricing, public positioning, UX choices that
differ only by taste, scope beyond the request) and for anything on the stop list.

## 0. Initialize project state

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/init_state.sh"
```

## 1. Classify the tier

Pick the tier from this rubric. The first matching row from the top wins; **when unsure, pick the
higher tier.** A tier can be raised later, never lowered.

| Tier | Pick when |
|---|---|
| **T3 live** | The change mutates live state: migrations or production data, deploy/cron/services, credentials, deleting or backing up data, money, messages to real people. |
| **T2 design** | A new concept, data model or cross-component contract; ambiguous intent; or two or more reasonable designs. |
| **T1 feature** | A new, bounded, reversible behaviour inside the existing model. |
| **T0 fix** | Existing behaviour is wrong and can be reproduced. |

Then let the resolver apply the hard floors (a high-stakes path forces at least T1; a live-state path
or trigger forces T3) and any `--tier` the operator gave:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/tier.py" resolve --paths <files you expect to touch> \
  --proposed <T0..T3> [--tier <operator's --tier>] [--live-state "<what live state it touches>"] \
  --reason "<one line: the rubric row that decided it>"
```

- **Exit 0:** print its two lines as the announcement (`Tier T1 (feature): …` / `Floor …`) and go on.
- **Exit 3:** the operator's `--tier` is below a hard floor. Say so with the resolver's reason and
  continue at the floor tier. Never honour it silently.
- **Exit 2:** the resolver could not decide (for example a configured path list is missing). Stop
  and report it; never fall back to T0.

Pass `--live-state` whenever the T3 row matched on intent rather than on a path, so the operator
cannot lower it by accident.

## 2. Read accumulated knowledge

Read the project's knowledge, backend-aware (invalid `GOODFELLOW_MEMORY` hard-errors here):

```bash
MODE=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory_config.py" resolve-mode) || { echo "$MODE"; exit 1; }
if [ "$MODE" = "rich" ]; then
  # Full MEMORY.md index (incl. ## Pending (unconfirmed), which you DISCOUNT as
  # unconfirmed). Internally applies the ordered fallback: .migrating -> knowledge.md
  # (no regen), absent -> knowledge.md, dirty/stale -> regenerate, else read index.
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory_index.py" --root .goodfellow read-index
else
  # flat mode (default): read .goodfellow/knowledge.md — all sections (Principles, Patterns, Gotchas)
  cat .goodfellow/knowledge.md 2>/dev/null || true
fi
```

In rich mode, auto-pull the full bodies of facts whose `domain` matches the topic. With no knowledge
yet, skip silently. Then read the plugin-shipped design principles (the web supplement loads only
when `GOODFELLOW_PRINCIPLES_WEB=1` or a `package.json` is at the project root; an invalid value
hard-errors here):

```bash
# Progressive disclosure (docs/instruction-density-budget.md): inject only the
# principle INDEX (P-NNN id + title + one-line rule), NOT the full bodies. The full
# corpus is ~17k tokens / ~300 directives — well past the accuracy-erosion ceiling.
# All error handling stays in Python (bad config / missing core / unreadable file ->
# non-zero exit + stderr); a non-zero exit means a config/packaging problem — stop.
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/principles_context.py" --index --project-root .
```

For a relevant category, expand it with
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/principles_context.py" --category NAME --project-root .`, and
pull a full principle with `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/principles_context.py" --show P-NNN [P-NNN ...] --project-root .`
before citing it. Internalize silently; let the principles shape the design.

## 3. `--from-loop N`

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/loop_store.py" --root . list
```

Use loop N's title and description as the seed. If it does not exist, say so and stop.

## 4. Route by tier

- **T0 fix.** No documents. Reproduce the bug with a failing test first (the red must be an
  assertion about the wrong behaviour, not a missing symbol), make the smallest fix, then run
  `/goodfellow:ship --previous T0`.
- **T1 feature.** No spec. Write a short plan in chat, three headings: **Goal**, **Approach**,
  **Tests** (each new test and its expected red). It goes into the PR body at ship. Build it
  test-first, then run `/goodfellow:ship --previous T1`.
- **T2 design and T3 live.** Continue with §5 to §7: questions, approaches, a spec, then review.

## 5a. Questions (default mode)

Ask only what changes the design and cannot be read from the code: at most three, in one message,
each with a recommended answer so a one-word reply works. Under autopilot, ask only the product
calls; decide the rest yourself and record them in the spec frontmatter as `assumptions:`.
**Dry-run:** ask nothing; record open questions as `unresolved_questions:`.

## 5b. `--grill` (interview mode)

For fuzzy or high-stakes intent, when the operator asks to be grilled (`--grill`, "grill me on X",
"interview me about X"). Never chosen automatically. Read `grill.md` in this skill's directory and
follow it: a bounded fact-finding pass, then one question at a time until no decision is open.
`--grill` is an explicit request for questions, so it interviews even under autopilot; only dry-run
skips the interview.

## 6. Approaches

Propose two or three approaches and lead with your pick: "**My pick: B** because <reason>.
(A trims X; C phases Z.)" Under autopilot, take the highest-conviction approach and record the
others as `rejected_alternatives:`, unless the choice between them is a product call.

## 7. Write the spec, then review it

Path: `docs/specs/<slug>-design.md`. Validate the slug before it touches a path: lowercase, fold
anything outside `[a-z0-9-]` to `-`, collapse and trim dashes, and **halt** if it still does not
match `^[a-z0-9]+(-[a-z0-9]+)*$`. Never overwrite an existing spec: publish with an exclusive
create (write a temp file in the same directory, then `ln` it to the target, retrying
`-2`, `-3`, … on collision).

Frontmatter: `title`, `status: draft`, `date`, **`tier`** (from §1), `confidence`
(`high` = precedent in the knowledge file; `medium` = novel; `low` = open questions that affect
architecture), `related_principles`, plus `assumptions` / `unresolved_questions` /
`rejected_alternatives` when present.

**T3 specs** add a **Rehearsal** section: which real mutations will be performed against which
sandbox the user supplies (a database copy, a temporary tree, a namespace), and what evidence the
PR will carry. goodfellow does not build the sandbox; it insists the rehearsal happens.

Do a self-review pass for internal contradictions, then in the same turn: emit a one-line summary
(path, tier, what it commits to) and dispatch `/goodfellow:review-doc --spec <spec-path>`. No gate.

**Dry-run:** log `{"event": "approach_selected", "would_act": true, …}` and
`{"event": "would_dispatch", "skill": "review-doc"}` to the run log
(`RUN_LOG=$(bash "${CLAUDE_PLUGIN_ROOT}/scripts/run_log.sh")`) instead of writing or dispatching.
