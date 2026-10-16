## 3. `--from-loop N`

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/loop_store.py" --root . list
```

Use loop N's title and description as the seed. If it does not exist, say so and stop.

## 4. Route by tier

- **T0 fix.** No documents. Reproduce the bug with a failing test first (the red must be an
  assertion about the wrong behaviour, not a missing symbol), make the smallest fix, then run
  `/goodfellow:ship --previous T0`.
- **T1 feature.** No spec. Write a short plan in chat, three headings: **Goal**, **Approach**,
  **Tests** (each new test and its expected red). It goes into the PR body at ship. Build it
  test-first, then run `/goodfellow:ship --previous T1`.
- **T2 design and T3 live.** Continue with §5 to §7: questions, approaches, a spec, then review.

**Dry-run (`GOODFELLOW_AUTOPILOT=dry-run`):** after the tier announcement, write nothing (no test,
no code, no spec). Log `{"event": "would_route", "would_act": true, "tier": "<Tn>"}` to the run
log and stop.

