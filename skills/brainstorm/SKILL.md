---
name: brainstorm
description: Design exploration with knowledge compounding — reads accumulated principles before proposing approaches, writes spec, auto-dispatches spec-review. Accepts --from-loop N to seed from a tracked follow-up.
---

The operator wants a design brainstorm. Run a streamlined exploration that compounds on prior knowledge.

## 1. Initialize project state

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/init_state.sh"
```

This ensures `.goodfellow/` exists and is gitignored.

## 2. Read accumulated knowledge

Before proposing approaches, read the project's accumulated design knowledge:

1. Read `.goodfellow/knowledge.md` — all sections (Principles, Patterns, Gotchas)
2. If the file doesn't exist, skip silently — first chain run starts empty
3. Read the plugin-shipped universal design principles (the web supplement is read only when web context is opted in — `GOODFELLOW_PRINCIPLES_WEB=1` or a `package.json` at the project root; an invalid value hard-errors here):

```bash
for f in $(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/principles_context.py" --project-root .); do
  cat "${CLAUDE_PLUGIN_ROOT}/knowledge/$f"
done
```

Internalize silently. Don't list principles back to the operator. Let them shape the design.

## 3. Resolve --from-loop sourcing

If the operator's prompt starts with `--from-loop <N>`:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/loop_store.py" --root . list
```

Find loop N, use its title + description as the brainstorm seed. If not found, tell the operator.

## 4. Clarifying questions (max 3, asked once)

- Pick only questions whose answers genuinely change the design
- Skip questions answerable by reading the codebase
- Bundle into one message

**Autopilot mode (`GOODFELLOW_AUTOPILOT=1` or `dry-run`):** skip questions entirely. Record unresolved questions in spec frontmatter as `unresolved_questions:`.

## 5. Propose approaches

Present 2-3 high-level approaches with a strong recommendation:

**Required format:** "**My pick: C** because <reason>. (A trims X; B phases Z.)" — lead with the pick.

**Scope bias: most-ambitious is the default.** Frame slim options as cut-downs from the ambitious default.

**Autopilot:** pick the highest-conviction approach. Record rejected alternatives in spec frontmatter as `rejected_alternatives:`.

## 6. Write the spec

After the operator picks (or autopilot self-picks), write the full design document in one pass:

- Path: `docs/specs/<slug>-design.md`
- Include proper frontmatter: title, status (draft), date, confidence (high/medium/low), related_principles
- **Confidence field:** `high` if approach has precedent in knowledge file, `medium` if novel, `low` if any unresolved_questions affect architecture (system boundaries, data flow, source-of-truth)
- Self-review pass: catch internal inconsistencies before showing the operator

**Autopilot dry-run (`GOODFELLOW_AUTOPILOT=dry-run`):** log `{"event": "approach_selected", "would_act": true, ...}` to `.goodfellow/runs/<timestamp>.jsonl`. Do NOT write the spec or dispatch spec-review. Log `would_act` for each mutation.

## 7. Auto-dispatch spec-review

After writing the spec, in the same turn:
1. Emit a brief summary (file path, what it commits to)
2. Dispatch `/goodfellow:spec-review <spec-path>`

No gate. No "Want me to proceed?" The operator can interrupt if the spec is obviously broken.

**Dry-run:** log `{"event": "would_dispatch", "skill": "spec-review"}` instead of dispatching.
