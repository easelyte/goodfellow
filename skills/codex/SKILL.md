---
name: codex
description: Direct Codex adversarial review on the current diff, a specific file, or a commit. Two-stage generator+judge pipeline; falls back to a single Claude reviewer when Codex is unavailable.
---

Run a Codex adversarial review.

## Purpose and review model

Run the standalone review requested in `$ARGUMENTS`.
Only when explaining model choice or degradation, read `background.md` in this skill's directory.
It describes the independent model, generator and grounding judge pipeline.
Judge or validation problems preserve unjudged findings with a degradation banner.
Claude fallback is unjudged and must say so in the output.

## Choose the target

At step 1 read `dispatch.md` in this skill's directory for target selection and exact bridge commands.
No arguments or `--uncommitted` reviews uncommitted changes.
Use `--commit <sha>` for one commit and `--base <branch>` for a branch diff.
Match the requested scope before dispatching.
The bridge handles Codex detection and Claude fallback.
`GOODFELLOW_REVIEW_MODEL` reaches only the Claude fallback, never Codex.

## Dispatch and validate output

At step 2 follow `dispatch.md` in this skill's directory and run the appropriate bridge command.
Check the exit code before treating stdout as a path.
On success the last stdout line is the readable review-artifact path.
On failure the bridge exits nonzero and emits `REVIEW_FAILED <rc> <class>`.
Reject that prefix before reading anything as a file.
A nonzero run is a failed review, never clean or LGTM.
Surface the failure and stop; never report “no findings” from a failed run.

## Present the findings

At step 3 read `findings.md` in this skill's directory and then the successful artifact.
Present blockers, majors and minors with their evidence.
Include the Judge audit showing which findings were kept or dropped.
If degraded or on Claude fallback, visibly state that the pass was UNJUDGED.
Do not hide unjudged findings because a grounding stage failed.

## Standalone boundary

This skill presents review results; it does not run the ship loop.
Do not file follow-up loops from this standalone command.
Use `/goodfellow:ship` when the operator wants the full delivery workflow.
