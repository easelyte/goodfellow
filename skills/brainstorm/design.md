## 5a. Questions (default mode)

Ask only what changes the design and cannot be read from the code: at most three, in one message,
each with a recommended answer so a one-word reply works. Under autopilot, ask only the product
calls; decide the rest yourself and record them in the spec frontmatter as `assumptions:`.
**Dry-run:** ask nothing; record open questions as `unresolved_questions:`.

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
(path, tier, what it commits to) and dispatch `/goodfellow:review --spec <spec-path>`. No gate.

**Dry-run:** log `{"event": "approach_selected", "would_act": true, …}` and
`{"event": "would_dispatch", "skill": "review"}` to the run log
(`RUN_LOG=$(bash "${CLAUDE_PLUGIN_ROOT}/scripts/run_log.sh")`) instead of writing or dispatching.
