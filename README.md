# Goodfellow

> Named after Ian Goodfellow, who invented adversarial networks. Your good fellow for shipping code.

An opinionated development lifecycle for Claude Code.
Your system gets smarter every time you ship.

```
    +-------------+     +------+     +------+     +---------+     +------+
    |  Brainstorm  |---->| Spec |---->| Plan |---->| Execute |---->| Ship |
    +-------------+     +------+     +------+     +---------+     +------+
           |                |            |              |              |
           |           spec-review  plan-review     verify        review
           |            + research   + research                  PR + merge
           |                                                          |
           +--- reads knowledge ------------------------------ writes +
    
                   your system gets smarter every cycle
```

Adversarial review at every stage. Knowledge that compounds. Nothing that slips.

## Why Goodfellow?

Every chain run extracts what you learned and feeds it into the next one.
Safety-critical deferred findings become tracked loops — triaged, not forgotten.
Polish-tier findings go to your knowledge file as gotchas.
Your 50th feature ships with the wisdom of the first 49.

**What's different:**
- **Multi-model adversarial review** — Claude + Codex/GPT reviewers catch what single-model review misses
- **Research injection** — factual claims in review findings are verified via web search before acting on them
- **Verifier pass** — before fixing a round 2+ finding, checks if it's still real. Prevents infinite fix-find-fix loops
- **Knowledge compounding** — `.goodfellow/knowledge.md` accumulates principles, patterns, and gotchas across chain runs
- **Follow-up tracking** — safety-critical deferred findings become loops in `.goodfellow/loops.json`, triaged with a two-reviewer system; polish-tier goes to knowledge gotchas

## Install

**From git (recommended for now):**
```bash
git clone https://github.com/easelyte/goodfellow.git ~/.claude/plugins/goodfellow
```

Claude Code auto-discovers plugins in `~/.claude/plugins/`. Restart your session after cloning.

**From marketplace** (when published):
```
claude plugin install easelyte/goodfellow
```

**Session-only** (for testing, no persistent install):
```
claude --plugin-dir /path/to/goodfellow
```

## Quick Start

```bash
# 1. Start a design
/goodfellow:brainstorm "Add user authentication with OAuth"

# 2. Each skill auto-dispatches the next:
#    brainstorm -> spec-review -> plan -> plan-review -> execute -> ship
#    Rounds 1-3 are gateless; round 4+ asks once. Knowledge compounds across runs.

# 3. Check what accumulated
cat .goodfellow/knowledge.md

# 4. Close the session cleanly
/goodfellow:close
```

## The Knowledge Loop

Every skill in the chain participates in a read-extract-persist cycle:

| Skill | Reads | Writes |
|---|---|---|
| brainstorm | All sections (Principles, Patterns, Gotchas) | — |
| spec-review / plan-review | Principles + Gotchas (flags violations against your project's accumulated knowledge) | — |
| execute | Gotchas (catches footguns at code-writing stage) | — |
| ship | — | New entries with `[pending]` tag |
| snap-compact | — | Extracts learnings before context loss |
| close | — | Promotes `[pending]` to confirmed |

The knowledge file (`.goodfellow/knowledge.md`) is append-only by default, human-curated, and intentionally unbounded. Entries follow a lightweight convention:

```markdown
## Principles
- 2026-06-02: Always validate at the boundary, never trust upstream sanitization

## Patterns
- 2026-06-02: Convergence-based review termination — stop when severity drops, not when count hits zero

## Gotchas
- [pending] 2026-06-02: The Codex CLI has no --file flag — use codex exec review with --commit/--base/--uncommitted
```

## Follow-Up Tracking

At ship time, deferred review findings are routed by severity:
- **Safety-critical (blocker/security/data-loss):** blocks PR creation. Must be fixed or explicitly waived by the operator. Filing as a loop is not sufficient.
- **Non-blocking deferred findings:** filed as loops in `.goodfellow/loops.json` for follow-up.
- **Polish-tier:** added to the knowledge file as gotchas (not filed as loops).

Spec-review and plan-review don't file loops — their unresolved findings carry forward to the next chain stage.

```bash
# File a follow-up manually
/goodfellow:ship  # (blocks on safety-critical, files non-blocking as loops)

# List open loops (inside Claude Code — CLAUDE_PLUGIN_ROOT is set automatically)
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/loop_store.py" list

# Seed a brainstorm from a loop
/goodfellow:brainstorm --from-loop 3

# Triage accumulated loops
/goodfellow:triage
```

**Priority scale:** p1 = critical, p2 = high, p3 = medium (default), p4 = low (round 4+ findings).

**Anti-whack-a-mole design:**
- Only safety-critical findings become loops; polish goes to knowledge gotchas
- Round 4+ findings file at lowest priority (except safety-critical)
- Soft cap warning at 15 active loops
- Triage has a 3-cycle hard cap on "still unclear" — forces a decision

## Autopilot Mode

```bash
# Full auto — chain runs hands-off with strategic halts
GOODFELLOW_AUTOPILOT=1

# Dry run first — logs decisions without mutating project code
# (the decision log itself IS written to .goodfellow/runs/ — that's the audit trail)
GOODFELLOW_AUTOPILOT=dry-run
```

**Strategic halts** — the system stops itself when it should:
- `confidence: low` in spec frontmatter (architecture-changing unknowns)
- Verifier flags >50% of findings as stale/noise in a round
- Unresolved questions that would change the architecture

Decision log written to `.goodfellow/runs/<timestamp>.jsonl` for auditability.

## Triage

When loops accumulate, `/goodfellow:triage` helps separate real defects from noise:

1. Two independent reviewers assess each loop (Claude + Codex when available, single Claude otherwise)
2. Reconciliation: both agree (high confidence), one opinion + one unclear (medium), disagree (low)
3. Operator confirms/overrides in a batch table
4. Decisions logged to `.goodfellow/triage-log.jsonl` for calibration

## Skills Reference

### The Chain (6 skills)

| Skill | Invocation | Codex? | Autopilot? |
|---|---|---|---|
| brainstorm | `/goodfellow:brainstorm [--from-loop N]` | No | Picks own approach |
| spec-review | `/goodfellow:spec-review <path>` | Optional | Full loop |
| plan | `/goodfellow:plan <spec-path>` | No | Auto-dispatches |
| plan-review | `/goodfellow:plan-review <path>` | Optional | Full loop |
| execute | `/goodfellow:execute <plan-path>` | Optional | All tasks |
| ship | `/goodfellow:ship [--quick]` | Optional | Full auto |

### Review and Triage (2 skills)

| Skill | Invocation | Codex? |
|---|---|---|
| codex-review | `/goodfellow:codex-review` | Optional (single-Claude fallback) |
| triage | `/goodfellow:triage` | Optional |

### Session Lifecycle (4 skills)

| Skill | Invocation |
|---|---|
| snap-compact | `/goodfellow:snap-compact` |
| close | `/goodfellow:close` |
| branch | `/goodfellow:branch <topic>` |
| prune-stale | `/goodfellow:prune-stale` |

## Codex Integration (Optional)

The [Codex CLI](https://github.com/openai/codex) is where the real adversarial value lives. When present, review skills run cross-model review (Claude + Codex/GPT) — two different model families catching different defect classes. Without Codex, reviews fall back to a single Claude reviewer. It still works, but you lose the cross-model diversity that makes adversarial review genuinely adversarial.

**Recommended setup:** Opus as your main session, Codex + Sonnet as the reviewers. Three perspectives: Opus reconciles, Sonnet reviews, Codex catches what Claude misses. For the full benefit, [install the free Codex CLI](https://github.com/openai/codex).

These are practical recommendations from daily use, not formal benchmarks.

```bash
# Force-disable Codex even when installed
GOODFELLOW_CODEX=0

# Set the Claude reviewer model
GOODFELLOW_REVIEW_MODEL=sonnet  # Default — cost-effective with Codex
GOODFELLOW_REVIEW_MODEL=opus    # Stronger single reviewer when no Codex
GOODFELLOW_REVIEW_MODEL=haiku   # Quick passes on small diffs
```

**How the reviewers compose:**

| Codex | GOODFELLOW_REVIEW_MODEL | What runs | Notes |
|---|---|---|---|
| Present | sonnet (default) | Sonnet + Codex | Recommended: cross-model diversity |
| Present | opus | Opus + Codex | Maximum review depth |
| Absent | opus | Single Opus reviewer | Best without Codex |
| Absent | sonnet | Single Sonnet reviewer | Budget option |

## Philosophy

- **Cross-model review catches what single-model misses** — different training lineages find different defect classes; same-model duplication adds little
- **Research injection grounds findings in verified facts** — factual claims are web-searched, not assumed
- **Convergence-based, not round-count-based** — stop when severity drops, not at an arbitrary round number
- **Verifier-before-fix prevents infinite loops** — don't burn fix cycles on stale findings
- **Knowledge should compound** — every chain run makes the next one better
- **Follow-ups need tracking, not just noting** — deferred findings become loops, triaged, not forgotten
- **Sessions deserve closing rituals** — persist learnings, check loops, clean up

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GOODFELLOW_AUTOPILOT` | unset | `1` for full auto, `dry-run` for observe mode |
| `GOODFELLOW_CODEX` | `1` | `0` to force-disable Codex |
| `GOODFELLOW_REVIEW_MODEL` | `sonnet` | Claude reviewer model: `sonnet`, `opus`, `haiku` |
| `GOODFELLOW_TAVILY_KEY` | unset | Tavily API key for batch research verification (optional — falls back to WebSearch) |
| `GOODFELLOW_TRIAGE_RETENTION_DAYS` | `90` | Days to keep closed-loop triage entries |
| `GOODFELLOW_RUNS_RETENTION_DAYS` | `90` | Days to keep autopilot run logs |

## Platform Support

**Best on macOS and Linux.** On Unix, the loop store uses `fcntl` file locking for concurrent session safety. Windows works for single-session use, but file locking is skipped — avoid running multiple Goodfellow sessions on the same project simultaneously (duplicate loop IDs possible).

**Worktree-first execution recommended** — `/goodfellow:execute` nudges you to use `/goodfellow:branch <topic>` before execution. This isolates feature work from your main workspace, keeps your root clean, and avoids the Windows-specific issue where Codex temp folders require admin rights to delete after a session ends. The nudge is advisory, not mandatory.

## Contributing

Contributions welcome. Please run the test suite before submitting:

```bash
cd scripts && python -m pytest -v
```

## License

MIT
