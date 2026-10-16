---
name: publish
description: Pre-open gate + cross-fork mechanics for opening a PR to a PUBLIC or not-solely-owned repo (a fork → upstream, an OSS contribution, any world-visible repo). Runs a user-configurable internal-ref scrub, a contributor checklist, and the correct cross-fork `gh pr create`. Not for your own private-workflow PRs — use `ship` for those.
---

Open a PR to a repo you do **not** solely control — a fork → upstream, an OSS
contribution, or any public repo — meeting the maintainer's bar BEFORE they bounce
it back. Skip this for your own private-workflow PRs (use `ship`).

## Scope

Use for a public repo, fork-to-upstream contribution or target not solely controlled by you.
Use `/goodfellow:ship` for your own private-workflow PRs.
Meet the maintainer's bar before opening the PR.

## Scrub before opening

At step 1 read `scrub.md` in this skill's directory for the blocking internal-reference scrub and exact command.
Use the user's denylist precedence; the plugin ships no project-specific inventory.
Scrub every internal reference in added lines and rerun until clean.
Never open with an unresolved hit; a partial scrub is not acceptable.
No denylist passes with a note unless `--require-denylist` requests fail-closed behavior.
Exit 2 is an uncomputable check, never a clean diff.

## Prepare the mergeable unit

At steps 2–4 read `checklist.md` in this skill's directory before finalizing the branch and description.
Ship the product change and tests; park supporting cargo on a separate branch.
Keep the parked material and say where it is in the PR body.
Every description claim must match the pushed behavior.
Build missing claimed behavior or correct the description before opening.
Run the full project verification and inspect the actual diff.
Surface cross-PR dependencies and graceful degradation explicitly.

## Pin cross-fork mechanics

At step 5 read `opening.md` in this skill's directory before pushing or constructing `gh pr create`.
For upstream targets pin repo, base and fork-owner head; implicit defaults can be wrong.
Base off upstream's default branch and expect squash merge.
Verify push access; never force-push another owner's branch or push a shared default branch.
Same-owner public PRs use normal same-repo mechanics but still require the scrub.

## Open and record

At step 6 follow `opening.md` in this skill's directory: describe what, why, verification, dependencies and parked cargo.
Open using the correct pinned flags, then stop for maintainer review before merge.
When called by ship for a public/upstream target, read its optional integration section too.
The scrub is a pre-open gate and must pass before PR creation.
