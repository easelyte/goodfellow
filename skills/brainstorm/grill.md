# brainstorm --grill: the interview

A relentless, one-question-at-a-time interview that drives a fuzzy or high-stakes idea to a
resolved design before any spec is written. It replaces §5a of `SKILL.md`; §6 and §7 (approaches,
spec, review) follow it as usual.

Attribution: the interview philosophy ("interview relentlessly, one question at a time, look up
facts rather than ask, reserve questions for human judgment") adapts Matt Pocock's `grilling` skill.

## 1. Scout before asking

"Look up facts, don't ask." Open with one focused fact-finding pass of **at most 8 tool calls**:
the code, existing specs and the knowledge file that bear on the idea. It runs in the foreground;
the operator can interrupt it. An empty or failed scout does not abort the interview, it just
leaves more to ask.

## 2. The interview loop

- **One question per message.** Ask, then wait. Never two at once.
- **Every question carries a recommended default** (answerable with one word) **and the exit**:

  > **Q4.** Should the importer dedupe on email or on external id?
  > *Recommended: external id (stable across email changes).*
  > *(Say "enough / write it" anytime to stop here and write the spec.)*

- **Dependency order.** Settle foundations (data model, source of truth, system boundaries) before
  details. A later question exists only because of an earlier answer.
- **Look up, don't ask.** Anything the scout or a tool can answer is looked up silently.
- **Ledger.** Each turn shows a compact ledger of resolved and open decisions with the running
  count, e.g. `Ledger: 3 resolved · 2 open · Q5 asked`. It is the convergence signal and, for
  metered plans, a live cost signal.
- **Termination is the ledger, not a vibe.** Stop when the open-decision ledger is empty: every
  foundational and dependent decision resolved **and** the last answer opened no new branch. There
  is no question cap. On "enough / write it", stop at once and carry the open decisions into
  `unresolved_questions`.

Announce the outcome, then continue with `SKILL.md` §6.

## 3. Extra spec frontmatter

A spec written after a grill carries, in addition to §7's keys:

```yaml
confidence_basis: grill
interview_rounds: <int>             # question -> answer exchanges; the scout pass is not counted
unresolved_questions: []            # non-empty only after an early "enough / write it"
# pending-review recovery: written with the spec, cleared by review-doc on a resolved review
review_status: pending
failed_reviewers: []
resume: "/goodfellow:review-doc --spec docs/specs/<slug>-design.md"
```

`confidence`: `high` when the ledger emptied and the approach has precedent; `medium` when it
emptied but the approach is novel; `low` when the operator cut it short with material questions
open.

If a reviewer fails or times out, append its id to `failed_reviewers` by rewriting the file to a
same-directory temp and renaming it over the original (never an in-place partial edit). The
recovery keys make a crash between writing the spec and reviewing it resumable.

## 4. Dry-run

Under `GOODFELLOW_AUTOPILOT=dry-run` there is no interview and nothing is written. Log what would
happen to the run log, one JSONL event each:

```bash
RUN_LOG=$(bash "${CLAUDE_PLUGIN_ROOT}/scripts/run_log.sh")
printf '%s\n' '{"event":"would_write_spec","would_act":true,"path":"docs/specs/<slug>-design.md","confidence":"low"}' >> "$RUN_LOG"
printf '%s\n' '{"event":"would_dispatch","would_act":true,"skill":"review-doc"}' >> "$RUN_LOG"
```
