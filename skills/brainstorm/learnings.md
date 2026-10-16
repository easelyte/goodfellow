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

