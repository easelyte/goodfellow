---
name: brainstorm
description: "Entry point for new work. Classifies the change into a risk tier (T0 fix … T3 live state), then does only the design work that tier needs — none for a fix, a short plan for a feature, a reviewed spec for a design. Reads accumulated principles first. Use when someone says \"brainstorm X\", \"design X\", \"let's build X\", \"scope X\" or \"help me think through X\". `--from-loop N` seeds from a tracked follow-up; `--tier Tn` overrides the tier (never below its hard floor)."
---

Start the work described in: $ARGUMENTS

For a one-question-at-a-time interview, use `/goodfellow:grill`.

Flags: `--from-loop N` (§3), `--tier T0..T3` (operator override,
§1). Autopilot is on by default (`GOODFELLOW_AUTOPILOT=0` turns it off): no approval gates between
steps. It still stops for **product calls** (naming, pricing, public positioning, UX choices that
differ only by taste, scope beyond the request), and the stop list asks before a force-push,
a default-branch push, a release or a publish.

## Start and classify

Read `setup.md` in this skill's directory now, before any project action: initialize state and classify T0–T3.
Announce the resolver's tier and floor. Raise tiers only; never lower them.
Exit 3 continues at the hard floor with an explanation; exit 2 stops the chain.
Product calls still require the operator; autopilot otherwise proceeds without gates.

## Read knowledge

At step 2, read `learnings.md` in this skill's directory and execute its backend and principle-index commands.
Invalid configuration or unreadable required principles stop the run.
Discount pending facts; expand relevant categories and full principles before citing them.

## Seed and route

At steps 3–4, read `routing.md` in this skill's directory for loop lookup, tier routing and dry-run logging.
A missing requested loop stops the run. T0 and T1 require assertion-red tests first.
T0 takes the smallest fix; T1 records Goal / Approach / Tests for the PR body.
Dry-run logs the intended route and stops without tests, code or documents.

## Design only when needed

Only for T2 or T3, read `design.md` in this skill's directory when reaching questions, approaches and the spec.
Ask at most three design-changing questions once; autopilot asks only product calls.
Use `/goodfellow:grill` when the operator wants a one-question-at-a-time interview.
Record assumptions and rejected alternatives; do not guess product decisions.

## Write and hand off

Follow `design.md` in this skill's directory for slug validation, exclusive creation and complete frontmatter.
Write only `docs/specs/<slug>-design.md`; never overwrite a prior spec.
T3 needs a rehearsal against a user-supplied sandbox with evidence for the PR.
Self-review contradictions, report the path and tier, then dispatch `/goodfellow:review --spec`.
Dry-run logs the approach and dispatch instead of writing or dispatching.
