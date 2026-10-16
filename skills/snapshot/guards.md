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
