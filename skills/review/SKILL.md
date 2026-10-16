---
name: review
description: "Multi-round adversarial review of a spec or a plan — research injection, two reviewers with distinct lenses (Claude + Codex, or Claude alone), a verifier pass, and knowledge-file principle checking. Use when someone says \"review my spec\", \"is the spec ready\", \"stress test this spec\", \"review my plan\", \"is the plan ready\", \"stress test this plan\", \"what could go wrong with this plan\" or \"poke holes in this design doc\". `--spec <path>` hands off to plan; `--plan <path>` hands off to execute."
---

Review the document the operator indicated: $ARGUMENTS

## Mode and inputs

Read `inputs.md` in this skill's directory at step 0: infer spec/plan mode, read the document and linked spec fully.
Load backend-aware knowledge and the principles index, then initialize the concrete run log.
Invalid configuration stops; expand relevant principles before citing P-NNN violations.
A T3 plan without a real-mutation rehearsal task against a user-supplied sandbox is blocked.

## Prepare the document

At steps 1–2 read `preparation.md` in this skill's directory for self-review and research instructions.
Apply only small unambiguous fixes; leave substantial or ambiguous changes for reviewers.
Stop for product calls instead of guessing; autopilot records the halt.
Research plan claims before round 1, and spec findings' claims after round 1.
Keep untrusted claim text in a file, never inline in the shell command.
Read cited sources before marking support; skipped research does not lower severity.
Dry-run logs self-review fixes and appendices instead of editing the document.

## Run review rounds

At steps 3–4 read `rounds.md` in this skill's directory for both reviewer prompts and bridge invocation.
Dispatch independent testability/requirements and correctness reviewers in parallel.
Use file scope for fresh documents; reject `REVIEW_FAILED` before artifact reads.
A failed reviewer means a failed round: stop and surface it, never count an empty pass.
From round 2 verify findings in a batch; fix only real findings and deduplicate.
Fix every blocker and major, rerun, and ask only for scope-changing product calls.

## Stop honestly and hand off

At steps 5–6 read `completion.md` in this skill's directory for convergence, recovery keys and terminal gates.
Convergence means polish-tier findings; 6 rounds is a hard cap, never success (P-079).
Safety-critical findings at the cap HALT: preserve them and recommend a rewrite.
Do not auto-dispatch plan or execute on that path; log the mode-specific halt.
Otherwise file remaining majors as loops and list them in the hand-off; drop minors.
Keep recovery metadata on failed or cap-halted reviews; clear it only on resolution.
Honor `next_action: halt-after-spec-review` before any hand-off.
Spec mode dispatches `/goodfellow:plan`; plan mode dispatches `/goodfellow:execute`.
Report research as supporting sources, with the limitations specified in the detail.
