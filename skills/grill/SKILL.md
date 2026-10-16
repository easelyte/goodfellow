---
name: grill
description: 'Interview a fuzzy or high-stakes idea one question at a time until decisions are resolved. Use when someone says "grill me on X", "interview me about X" or "grill me". Scout facts first, keep a decision ledger, then write a spec and dispatch review.'
---

Interview the idea in: $ARGUMENTS

## Initialize and ground the topic

Read `../brainstorm/setup.md` relative to this skill's directory now for initialization and the unchanged tier classifier.
Then read `../brainstorm/learnings.md` relative to this skill's directory for backend-aware knowledge and principles.
Announce tier and floor, raise only, and stop on configuration or resolver failure.
This explicit interview request applies even under autopilot; never choose it automatically.

## Route before interviewing

Read `../brainstorm/routing.md` relative to this skill's directory after setup classification.
Honor `--tier` (raise only) and `--from-loop N`, including the missing-loop stop.
Follow brainstorm's tier routing: T0/T1 skip the interview, spec and review and go
straight through its test-first fix/short chat plan to ship; only T2/T3 continue below.
In dry-run, log its `would_route` event and stop before questions, writes or dispatch.

## Scout first

When reaching the scout, read `interview.md` in this skill's directory §1–2 before asking anything.
Use at most eight foreground tool calls to inspect relevant code, specs and knowledge.
An empty or failed scout leaves more questions; it does not abort the interview.
Look up facts silently and reserve questions for human judgment.

## Interview to a resolved ledger

Ask exactly one question per message, then wait for the answer.
Every question includes a recommended default and the “enough / write it” exit.
Resolve foundations before dependent details and show the running decision ledger each turn.
Stop only when no decision is open and the last answer created no new branch.
There is no question cap; on “enough / write it”, stop immediately and carry open questions forward.

## Approaches, spec and review

After the interview, read `../brainstorm/design.md` relative to this skill's directory §6–7 for approaches and spec creation.
Then read `interview.md` in this skill's directory §3 for the interview-specific frontmatter and recovery rules.
Validate the slug and create exclusively at `docs/specs/<slug>-design.md`; never overwrite.
Include tier, confidence basis, round count, unresolved questions and pending-review recovery.
T3 still requires the user-supplied sandbox rehearsal design.
Self-review and dispatch `/goodfellow:review --spec <spec-path>` in the same turn.
On reviewer failure, atomically record its ID; keep recovery metadata until review resolves.
