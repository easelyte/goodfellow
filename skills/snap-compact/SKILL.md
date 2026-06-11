---
name: snap-compact
description: Extract learnings from the session before context compaction — preserves knowledge that would otherwise be lost when the context window is compressed.
---

Extract learnings before compacting context.

## 0. Ensure state directory

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/init_state.sh"
```

## 1. Scan for learnings

Review the current session's work for:
- **Principles:** design rules that emerged
- **Patterns:** solutions that worked well
- **Gotchas:** footguns or surprising behaviors discovered

Focus on learnings from the current session that haven't been captured yet.

## 2. Persist to knowledge file

Resolve the backend mode first (invalid `GOODFELLOW_MEMORY` hard-errors here):

```bash
MODE=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory_config.py" resolve-mode) || { echo "$MODE"; exit 1; }
```

**flat mode (`MODE=flat`, default — behavior unchanged):** Append any found learnings to `.goodfellow/knowledge.md` with `[pending]` tag and date:

```
- [pending] 2026-06-02: <learning text>
```

If `.goodfellow/knowledge.md` doesn't exist, create it with the three section headers:

```markdown
## Principles

## Patterns

## Gotchas
```

Then append entries to the appropriate section.

**rich mode (`MODE=rich`):** skip restatements of shipped principles (cite `P-NNN`), then write each kept learning as a per-fact file (the CLI auto-migrates `knowledge.md` on first rich write):
```bash
PID=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/dedup_principles.py" --description "<learning text>" \
        --principles "${CLAUDE_PLUGIN_ROOT}/knowledge/principles.md" "${CLAUDE_PLUGIN_ROOT}/knowledge/principles-web.md")
# if $PID non-empty: skip, log "skipped (restates $PID)"; else:
# Valid as written — substitute your own values. --name is a kebab-slug matching
# [a-z0-9-]; --type is one of principle|pattern|gotcha; --domain is optional (omit if none):
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory_index.py" --root .goodfellow write-fact \
  --name validate-at-boundary --description "Always validate at the boundary" \
  --type principle --status pending --opened "$(date +%F)" --body "Detail of the learning."
```

## 3. Compact

Proceed with context compaction. The learnings are now persisted and will survive the context loss.

Report: "Extracted N learnings to .goodfellow/knowledge.md before compacting."
