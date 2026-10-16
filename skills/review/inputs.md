## 0. Mode and inputs

- **Mode.** `--spec <path>` or `--plan <path>`. Without a flag, infer it: a file under
  `docs/plans/` or with a `spec:` key in its frontmatter is a plan; anything else is a spec.
- **Tier.** Read `tier:` from the document's frontmatter (written by `brainstorm`). A spec or plan
  exists only at T2 and T3. At T3, a plan must contain a **rehearsal task**: perform the real
  mutations against a sandbox the user supplies (a database copy, a temporary tree, a namespace)
  and record the evidence for the PR. A T3 plan without one is a blocker.

Read the document fully (and, for a plan, its spec from the `spec:` frontmatter key). Then read the
project's accumulated knowledge, backend-aware (invalid `GOODFELLOW_MEMORY` hard-errors here):

```bash
MODE=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory_config.py" resolve-mode) || { echo "$MODE"; exit 1; }
if [ "$MODE" = "rich" ]; then
  # Full MEMORY.md index (incl. ## Pending (unconfirmed) — discount those). Internally
  # falls back: .migrating -> knowledge.md (no regen), absent -> knowledge.md, dirty/stale -> regenerate.
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory_index.py" --root .goodfellow read-index
else
  cat .goodfellow/knowledge.md 2>/dev/null || true   # flat: Principles + Gotchas inform principle checking
fi
```

In rich mode, auto-pull bodies of exact-`domain` matches; open other relevant fact bodies by name from the index.

Also read the plugin-shipped universal design principles and flag violations by their `P-NNN` id (the web supplement is read only when web context is opted in — `GOODFELLOW_PRINCIPLES_WEB=1` or a `package.json` at the project root; an invalid value hard-errors here):

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

Initialize the run log so every decision below has a concrete destination:

```bash
RUN_LOG=$(bash "${CLAUDE_PLUGIN_ROOT}/scripts/run_log.sh")
```

Use `$RUN_LOG` for every append in this skill, never a literal `<timestamp>.jsonl` placeholder.

