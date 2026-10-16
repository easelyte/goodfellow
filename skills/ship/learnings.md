## 4. Extract learnings to knowledge file

After the final review pass, scan the diff and review findings for new knowledge:

- **Principles:** design rules that emerged ("always validate at boundary X")
- **Patterns:** solutions that worked ("convergence-based termination")
- **Gotchas:** footguns discovered ("API returns null not undefined on empty")

Resolve the backend mode first (invalid `GOODFELLOW_MEMORY` hard-errors here):

```bash
MODE=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory_config.py" resolve-mode) || { echo "$MODE"; exit 1; }
```

**flat mode (`MODE=flat`, default — behavior unchanged):** Append candidates to `.goodfellow/knowledge.md` with `[pending]` tag and date:
```
- [pending] 2026-06-02: <learning text>
```

**rich mode (`MODE=rich`):** skip restatements of shipped principles (cite `P-NNN`), then write each kept candidate as a per-fact file:
```bash
# Fail CLOSED: a dedup error (drift / unparseable principles) must STOP, not silently
# persist a restatement. principles.md is required; the web supplement is optional.
DEDUP_FILES=( "${CLAUDE_PLUGIN_ROOT}/knowledge/principles.md" )
[ -f "${CLAUDE_PLUGIN_ROOT}/knowledge/principles-web.md" ] && DEDUP_FILES+=( "${CLAUDE_PLUGIN_ROOT}/knowledge/principles-web.md" )
PID=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/dedup_principles.py" --description "<learning text>" \
        --principles "${DEDUP_FILES[@]}") || exit 1
# if $PID non-empty: skip, log "skipped (restates $PID)"; else:
# Valid as written — substitute your own values. --name is a kebab-slug matching
# [a-z0-9-]; --type is one of principle|pattern|gotcha; --domain is optional (omit if none):
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory_index.py" --root .goodfellow write-fact \
  --name validate-at-boundary --description "Always validate at the boundary" \
  --type principle --status pending --opened "$(date +%F)" --body "Detail of the learning."
```

