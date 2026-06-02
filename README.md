# Shipline

An opinionated development lifecycle for Claude Code.
Your system gets smarter every time you ship.

```
Brainstorm --> Spec --> Plan --> Execute --> Ship
     \          |        |         |        /
      `---- knowledge compounds across runs ----'
```

With adversarial review, knowledge that compounds, and nothing that slips.

## Why Shipline?

Every chain run extracts what you learned and feeds it into the next one.
Every deferred finding becomes a tracked loop — triaged, not forgotten.
Your 50th feature ships with the wisdom of the first 49.

**What's different:**
- **Multi-model adversarial review** — Claude + Codex/GPT reviewers catch what single-model review misses
- **Research injection** — factual claims in review findings are verified via web search before acting on them
- **Verifier pass** — before fixing a round 2+ finding, checks if it's still real. Prevents infinite fix-find-fix loops
- **Knowledge compounding** — `.shipline/knowledge.md` accumulates principles, patterns, and gotchas across chain runs
- **Follow-up tracking** — deferred findings become loops in `.shipline/loops.json`, triaged with a two-reviewer system

## Install

```
claude plugin install easelyte/shipline
```

Or for development/testing:
```
claude --plugin-dir /path/to/shipline
```

## Quick Start

```bash
# 1. Start a design
/shipline:brainstorm "Add user authentication with OAuth"

# 2. The chain runs automatically:
#    brainstorm -> spec-review -> plan -> plan-review -> execute -> ship
#    Knowledge file created on first run, compounds on every subsequent run.

# 3. Check what accumulated
cat .shipline/knowledge.md

# 4. Close the session cleanly
/shipline:close
```

## The Knowledge Loop

Every skill in the chain participates in a read-extract-persist cycle:

| Skill | Reads | Writes |
|---|---|---|
| brainstorm | All sections (Principles, Patterns, Gotchas) | — |
| spec-review / plan-review | Principles + Gotchas (flags violations) | — |
| execute | Gotchas (catches footguns at code-writing stage) | — |
| ship | — | New entries with `[pending]` tag |
| snap-compact | — | Extracts learnings before context loss |
| close | — | Promotes `[pending]` to confirmed |

The knowledge file (`.shipline/knowledge.md`) is append-only by default, human-curated, and intentionally unbounded. Entries follow a lightweight convention:

```markdown
## Principles
- 2026-06-02: Always validate at the boundary, never trust upstream sanitization

## Patterns
- 2026-06-02: Convergence-based review termination — stop when severity drops, not when count hits zero

## Gotchas
- [pending] 2026-06-02: The Codex CLI has no --file flag — use codex exec review with --commit/--base/--uncommitted
```

## Follow-Up Tracking

Review findings that aren't fixed now become tracked loops in `.shipline/loops.json`:

```bash
# File a follow-up manually
/shipline:ship  # (auto-files safety-critical deferred findings)

# List open loops
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/loop_store.py" list

# Seed a brainstorm from a loop
/shipline:brainstorm --from-loop 3

# Triage accumulated loops
/shipline:triage
```

**Anti-whack-a-mole design** (lessons from production where 47% of filed loops never delivered value):
- Only safety-critical findings become loops; polish goes to knowledge gotchas
- Round 4+ findings file at lowest priority (except safety-critical — V9)
- Soft cap warning at 15 active loops
- Triage has a 3-cycle hard cap on "still unclear" — forces a decision

## Autopilot Mode

```bash
# Full auto — chain runs hands-off with strategic halts
SHIPLINE_AUTOPILOT=1

# Dry run first — see what it would do without mutating
SHIPLINE_AUTOPILOT=dry-run
```

**Strategic halts** — the system stops itself when it should:
- `confidence: low` in spec frontmatter (architecture-changing unknowns)
- Verifier flags >50% of findings as stale/noise in a round
- Unresolved questions that would change the architecture

Decision log written to `.shipline/runs/<timestamp>.jsonl` for auditability.

## Triage

When loops accumulate, `/shipline:triage` helps separate real defects from noise:

1. Two independent reviewers assess each loop (Claude + Codex or dual-Claude)
2. Reconciliation: both agree (high confidence), one opinion + one unclear (medium), disagree (low)
3. Operator confirms/overrides in a batch table
4. Decisions logged to `.shipline/triage-log.jsonl` for calibration

## Skills Reference

### The Chain (6 skills)

| Skill | Invocation | Codex? | Autopilot? |
|---|---|---|---|
| brainstorm | `/shipline:brainstorm [--from-loop N]` | No | Picks own approach |
| spec-review | `/shipline:spec-review <path>` | Optional | Full loop |
| plan | `/shipline:plan <spec-path>` | No | Auto-dispatches |
| plan-review | `/shipline:plan-review <path>` | Optional | Full loop |
| execute | `/shipline:execute <plan-path>` | Optional | All tasks |
| ship | `/shipline:ship [--quick]` | Optional | Full auto |

### Review and Triage (2 skills)

| Skill | Invocation | Codex? |
|---|---|---|
| codex-review | `/shipline:codex-review` | Required |
| triage | `/shipline:triage` | Optional |

### Session Lifecycle (4 skills)

| Skill | Invocation |
|---|---|
| snap-compact | `/shipline:snap-compact` |
| close | `/shipline:close` |
| branch | `/shipline:branch <topic>` |
| prune-stale | `/shipline:prune-stale` |

## Codex Integration (Optional)

The [Codex CLI](https://github.com/openai/codex) is optional. When present, review skills run dual-model adversarial review (Claude + Codex). When absent, they fall back to dual-Claude review with different system prompts.

```bash
# Force-disable Codex even when installed
SHIPLINE_CODEX=0

# Set the Claude reviewer model tier
SHIPLINE_REVIEW_MODEL=opus  # Recommended when no Codex — capability-tier diversity
SHIPLINE_REVIEW_MODEL=sonnet # Default — sufficient with Codex present
SHIPLINE_REVIEW_MODEL=haiku  # Quick passes on small diffs
```

## Philosophy

- **Multi-model review catches what single-model misses** — different post-training lineages find different defect classes
- **Research injection grounds findings in verified facts** — factual claims are web-searched, not assumed
- **Convergence-based, not round-count-based** — stop when severity drops, not at an arbitrary round number
- **Verifier-before-fix prevents infinite loops** — don't burn fix cycles on stale findings
- **Knowledge should compound** — every chain run makes the next one better
- **Follow-ups need tracking, not just noting** — deferred findings become loops, triaged, not forgotten
- **Sessions deserve closing rituals** — persist learnings, check loops, clean up

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `SHIPLINE_AUTOPILOT` | unset | `1` for full auto, `dry-run` for observe mode |
| `SHIPLINE_CODEX` | `1` | `0` to force-disable Codex |
| `SHIPLINE_REVIEW_MODEL` | `sonnet` | Claude reviewer model: `sonnet`, `opus`, `haiku` |
| `SHIPLINE_TRIAGE_RETENTION_DAYS` | `90` | Days to keep closed-loop triage entries |
| `SHIPLINE_RUNS_RETENTION_DAYS` | `90` | Days to keep autopilot run logs |

## Contributing

Contributions welcome. Please run the test suite before submitting:

```bash
cd scripts && python -m pytest -v
```

## License

MIT
