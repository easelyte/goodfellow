---
name: triage
description: Two-reviewer loop triage — independently assess each open loop, reconcile verdicts, batch operator confirmation. 3-cycle hard cap on unclear findings. Ground truth logged to triage-log.jsonl.
---

Triage open loops to separate real defects from noise.

## Load backlog

At steps 0–1 read `assessment.md` in this skill's directory, initialize state and list open loops.
If none are open, report that and exit.
Assess against cited code and recent git history, not just the original claim.

## Independent assessment

At step 2 follow the two-reviewer dispatch and bridge command in `assessment.md` in this skill's directory.
Reviewers are read-only; neither sees the other's output.
Dispatch a wave concurrently rather than serializing one loop at a time.
Above about 8–10 loops, use bounded waves and wait before launching the next.
Collect real-defect, not-a-defect or still-unclear plus each reviewer's reason.
Reject `REVIEW_FAILED` before any artifact read; failed assessment is never clean.
Surface failures and fall back to reviewer 1 or rerun rather than recording emptiness.

## Reconcile and require decisions

At steps 3–5 read `decisions.md` in this skill's directory for the reconciliation and batch table formats.
Agreement is high confidence; one opinion plus unclear is medium confidence.
Disagreement or two unclear votes remains unclear with low confidence.
After three prior unclear cycles, force MUST DECIDE: no further still-unclear vote.
Present all rows together and obtain operator confirmations, overrides or skips.
Never apply an unconfirmed decision.

## Persist confirmed outcomes

At steps 6–8 continue with `decisions.md` in this skill's directory for store commands and ground-truth schema.
Real defects stay open; close non-defects and increment unclear triage counts.
Append decisions to `.goodfellow/triage-log.jsonl` with locking, flush and fsync.
Copy the durable loop UUID; integer loop IDs can be reused after a store reset.
Carry the lens when present and summarize counts, including MUST DECIDE rows.

## Optional lens report

Only when accumulated decisions warrant the optional step 9, read `tuning.md` in this skill's directory.
Run the read-only report and read its caveats before interpreting it.
Deferred-loop disposition is not a lens false-positive rate; never weaken a lens on it alone.
Flags need the minimum sample; a human validates any tuning, and missed defects are out of scope.
