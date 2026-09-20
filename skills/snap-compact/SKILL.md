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

## 3. Snapshot the enforced guard set (governance survives, prose does not)

Compaction is optimized for task accuracy, so nothing measures whether a safety
constraint survives the rewrite ("Governance Decay"). A "never do X" *sentence* can
silently vanish across the boundary; a tool-layer guard cannot, because it lives on
disk and fires on every tool call. Snapshot the active BLOCK-rule set now so the
post-boundary session can **assert** it is still enforced instead of trusting the
summarizer:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/guard_engine.py" --selfcheck > .goodfellow/guard-set.pre-compact.json
```

This records the enabled built-ins, protected branches, and the ids of every
declarative rule in `.goodfellow/guards.json`. If it reports a non-null
`config_error`, fix `guards.json` *before* compacting (`guard_engine.py --validate`
prints the specific error) — a broken config means your project's own
expensive-to-reverse rules are NOT being enforced.

## 4. Compact

Proceed with context compaction. The learnings are now persisted and will survive the context loss.

Report: "Extracted N learnings to .goodfellow/knowledge.md before compacting."

## 5. After the boundary — assert the guard set is intact

On the first turn after compaction, assert the current guard set against the
pre-compaction snapshot. The snapshot records full per-rule digests (not just
ids), the enabled built-ins, the protected branches, and whether the hook is
still wired — so a rule that kept its id while its pattern changed, or a
`config_error` that appeared, or the registration disappearing, all count as
drift. The assertion exits non-zero on any mismatch (a gate, not a warning):

```bash
if ! python3 "${CLAUDE_PLUGIN_ROOT}/scripts/guard_engine.py" \
     --assert-guard-set .goodfellow/guard-set.pre-compact.json; then
  echo "STOP: guard set drifted across compaction — investigate before proceeding."
  exit 1
fi
echo "Guard set intact across the compaction boundary."
```

Do NOT wrap the assertion in `|| echo …` — that swallows the non-zero exit, so
the gate would report success precisely when governance drifted. Keep the failing
status: the set lives on disk, so it *should* match exactly, and if it does not,
your governance changed under you — stop and investigate rather than assuming the
summary kept it.
