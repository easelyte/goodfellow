## 6. Create PR

**Public / not-solely-owned target?** If this PR targets a repo you do not solely
control (a fork → upstream, an OSS contribution, any public repo), run the
`publish` gate FIRST — it scrubs the diff against your internal-ref denylist and
gives the correct cross-fork `gh pr create` mechanics. Skip it for your own repo.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/public_pr_scrub.py" --base "$BASE" || {
  echo "internal-ref scrub failed — do not open the public PR" >&2; exit 1; }
```

Create the PR with convergence and verifier stats in the description. State the review
exit honestly (P-079 — reaching a limit is not success): if round N was the hard cap with
findings still deferred, write "Halted at hard cap (round N)", not "Converged at round N".

```
## Summary
<what changed>

Tier: T1 (feature): <the resolver's reason>. Floor: <floor and why>.

## Plan (T1 only)
Goal / Approach / Tests, as written before building.

## Rehearsal (T3 only)
<the real mutations, the sandbox they ran against, and the result>

## Test evidence
- Red: <each new test and the assertion it failed with on the base> (red_check: N OK)
- Deliberate breaks: <break -> test that caught it>
- Mutation (high-stakes paths): K/M killed; <each survivor: killed by <test> or equivalent because <reason>> (or: not configured)

## Review stats
- Converged at round N, M findings resolved, K knowledge entries referenced
  (or: Halted at hard cap (round N) with D deferred findings — limit reached, not full resolution)
- Verifier: X findings verified, Y filtered (A stale, B noise)
- Knowledge: C new entries added ([pending])
- Loops: D follow-ups filed
```

## 7. Merge

**T3 with a pre-merge live step:** if the plan's `## Live step` must run before the merge (for
example a migration the merged code depends on), do §7a now and merge only after it is verified.

**Autopilot (default):** merge when the review converged with no unresolved blocker, the final-HEAD
check is clean and CI is green. A force-push, a push to the default branch, a tag or release and
a package publish ask the operator through Claude Code's confirmation, whatever the tier; their yes
goes through. **Dry-run:** log `would_act: merge` instead.
**`GOODFELLOW_AUTOPILOT=0`:** ask once whether to merge.

## 7a. T3 live step

Only after the review converged, the final-HEAD check is clean and CI is green (and, for a
post-merge step, after the merge): present the plan's `## Live step` (the exact commands, before or after the merge, verification, rollback) together
with the rehearsal evidence, and hand it to the operator, whatever the autopilot setting. The
operator decides when it runs: migrations, deploys, publishes and releases touch live state, and
sends or spending are never automated. Then verify the result as the plan says,
record it in the PR, and only then report the work done. **Dry-run:** log
`{"event": "would_hand_off_live_step", "would_act": true}`.
