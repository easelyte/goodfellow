---
name: ship
description: "Verify, review, create PR, extract learnings to knowledge file, file follow-up loops. --quick: single-round review for small diffs. Safety-critical findings block PR creation."
---

Ship the current work. Runs verify → review → PR → extract learnings → file loops.

## 0. Ensure state directory

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/init_state.sh"
```

## 1. Full verification pass

Run verification on the entire diff:

**Auto-detect toolchain** (same as execute):
- Python → ruff check + ruff format --check
- JS/TS → eslint or configured linter
- JSON → structural validation
- Tests → discover and run matching tests

If verification fails: surface errors, do not proceed to review.

## 2. Review

### Standard mode (default)
Multi-round adversarial review on the diff. Same convergence algorithm as spec-review/plan-review:
- Two reviewers per round (Claude + Codex/single-Claude fallback via bridge)
- Verifier pass at round 2+ (via `convergence_detector.py`)
- Research injection between rounds 1 and 2 if factual claims in findings
- Convergence when severity drops to polish-tier
- Hard cap 6 rounds

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/codex-bridge.sh" --kind diff --uncommitted
```

### Quick mode (`--quick`)
Single-round review for diffs <50 net changed lines. Safety-critical findings in quick mode still block PR and get filed as loops.

## 3. Ship-blocking check

**If any unresolved safety-critical finding remains at convergence or hard cap: HALT.** No PR creation, no merge. The finding must be fixed or the operator must explicitly waive it.

Filing as a loop is NOT sufficient for safety-critical findings at ship time.

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
PID=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/dedup_principles.py" --description "<learning text>" \
        --principles "${CLAUDE_PLUGIN_ROOT}/knowledge/principles.md" "${CLAUDE_PLUGIN_ROOT}/knowledge/principles-web.md")
# if $PID non-empty: skip, log "skipped (restates $PID)"; else:
# Valid as written — substitute your own values. --name is a kebab-slug matching
# [a-z0-9-]; --type is one of principle|pattern|gotcha; --domain is optional (omit if none):
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/memory_index.py" --root .goodfellow write-fact \
  --name validate-at-boundary --description "Always validate at the boundary" \
  --type principle --status pending --opened "$(date +%F)" --body "Detail of the learning."
```

## 5. File follow-up loops

Deferred findings from the convergence exit:

- **Safety-critical** → file to `.goodfellow/loops.json` via loop store. Priority from finding severity. Round 4+ findings at p4 unless safety-critical.
- **Polish-tier** → append to knowledge file as gotchas instead of filing loops

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/loop_store.py" --root . add "<title>" --priority <p> --source "ship-review-r<N>" --description "<text>"
```

Soft cap check: if >15 active loops, warn "loop backlog growing — consider /goodfellow:triage".

## 6. Create PR

Create the PR with convergence and verifier stats in the description:

```
## Summary
<what changed>

## Review stats
- Converged at round N, M findings resolved, K knowledge entries referenced
- Verifier: X findings verified, Y filtered (A stale, B noise)
- Knowledge: C new entries added ([pending])
- Loops: D follow-ups filed
```

## 7. Optional merge

In interactive mode: ask once whether to merge.
In autopilot mode: auto-merge (dry-run logs `would_act: merge` instead).
