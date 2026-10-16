---
name: ship
description: "Classify the risk tier, verify, prove new tests can fail, review the diff to convergence, open the PR, merge on autopilot, then extract learnings and file follow-up loops. --tier T0..T3 overrides the tier (never below its hard floor); --quick is an alias for --tier T0. Safety-critical findings block the PR."
---

Ship the current work: classify → verify → red check → review → PR → merge → learnings → loops.

**Dry-run (`GOODFELLOW_AUTOPILOT=dry-run`):** run the checks, but perform none of the writes below
(knowledge entries, loops, the PR, the merge). Log each as a `would_act` event to the run log
(`RUN_LOG=$(bash "${CLAUDE_PLUGIN_ROOT}/scripts/run_log.sh")`) instead.

Arguments: `[--tier T0..T3 | --quick] [--previous Tn]`. `--quick` means `--tier T0`. `--previous` is
the tier an earlier step of this run chose (brainstorm, or the `tier:` key of the plan or spec);
it can raise the tier, never lower it.

## Initialize and verify

Read `verification.md` in this skill's directory at steps 0–1a: initialize, classify the actual diff, verify, prove reds.
Announce tier and floor; exit 3 continues at the floor, exit 2 stops.
Tiers only rise. Caps remain 3 at T0/T1 and 6 at T2/T3; all checks still run.
T3 must have user-supplied sandbox rehearsal evidence before review or HALT.
Verification failures stop. Red-check exit 2 is not run, never passed.
Do not opt out of a failed sandbox yourself; report its message to the user.
Mutation breaks must use fakes or isolated resources, never live destructive targets.

## Review the committed branch

At steps 2–3, read `review.md` in this skill's directory for the review loop and final-HEAD commands.
Review the actual committed PR diff; an empty artifact for a nonempty diff fails.
Reject nonzero bridge exits and `REVIEW_FAILED` before reading any artifact.
Verify findings from round 2, fix substantive findings, and apply the existing caps.
A moved HEAD needs the final delta gate; unmapped rebases need a full branch review.
Unresolved safety-critical findings HALT PR and merge unless explicitly waived.
Filing a loop is never a substitute for this halt.

## Preserve findings and learnings

Only after review, read `learnings.md` in this skill's directory at step 4 for backend-aware pending writes.
Dedup errors fail closed; skip restatements of shipped principles.
At step 5 read `loops.md` in this skill's directory: blockers and majors go to loop store, minors to gotchas.
Use the judged finding's lens when available; warn above 15 active loops.

## Deliver and hand off live actions

At steps 6–7a read `delivery.md` in this skill's directory for the scrub, PR template, merge and live-step rules.
Public or not-solely-owned targets require running the publish scrub gate (`scripts/public_pr_scrub.py`) before opening.
Report hard-cap exits as limits (P-079), never as convergence.
Merge on autopilot only with converged review, clean final-HEAD check and green CI.
With autopilot off, ask once whether to merge. Respect stop-list confirmations.
T3 live commands go to the operator after review and CI, with rehearsal and rollback.
A pre-merge live step must be verified before merge; verify and record the result.
