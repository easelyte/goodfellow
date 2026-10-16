## 0. Worktree hygiene check

Before starting execution, check if you're running in the root workspace:

```bash
git rev-parse --show-toplevel
git worktree list
```

If the current directory IS the root workspace (not a worktree), warn:

> "Running in root workspace. For cleaner isolation (especially on Windows where Codex temp folders require admin rights to delete), consider `/goodfellow:branch <topic>` first, then execute from the worktree."

Proceed regardless — this is a nudge, not a gate.

## 1. Read the plan and knowledge

Read the plan file. Parse phases and tasks (headers: `## Phase N`, `### T-N.X`).

Read the project's accumulated knowledge, backend-aware (invalid `GOODFELLOW_MEMORY` hard-errors here):

```bash
MODE=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory_config.py" resolve-mode) || { echo "$MODE"; exit 1; }
if [ "$MODE" = "rich" ]; then
  # execute reads the FULL MEMORY.md index in rich mode (NOT a gotchas-only subset —
  # that would silently drop confirmed pattern/principle facts); it WEIGHTS gotchas/
  # principles at the code-writing stage. Includes ## Pending (unconfirmed) — discount those.
  # Internal fallback: .migrating -> knowledge.md (no regen), absent -> knowledge.md, dirty/stale -> regenerate.
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory_index.py" --root .goodfellow read-index
else
  cat .goodfellow/knowledge.md 2>/dev/null || true   # flat: Gotchas are known footguns to watch for
fi
```

In rich mode, auto-pull bodies of exact-`domain` matches; open other relevant fact bodies by name from the index.

Also read the plugin-shipped universal design principles and apply them at the code-writing stage (the web supplement is read only when web context is opted in — `GOODFELLOW_PRINCIPLES_WEB=1` or a `package.json` at the project root; an invalid value hard-errors here):

```bash
# Progressive disclosure (docs/instruction-density-budget.md): inject only the
# principle INDEX (P-NNN id + title + one-line rule), NOT the full bodies. The full
# corpus is ~17k tokens / ~300 directives — well past the accuracy-erosion ceiling.
# All error handling stays in Python (bad config / missing core / unreadable file ->
# non-zero exit + stderr); a non-zero exit means a config/packaging problem — stop.
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/principles_context.py" --index --project-root .
```

Scan the category routing table. For any category relevant to this work, expand its one-liners with
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/principles_context.py" --category NAME --project-root .`, then pull the
full body of a relevant principle with `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/principles_context.py" --show P-NNN [P-NNN ...] --project-root .`
before applying or citing it — requesting a parent id (e.g. `P-017`) includes its
sub-principles. Cite violations by P-NNN.

