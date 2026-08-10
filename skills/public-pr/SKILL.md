---
name: public-pr
description: Pre-open gate + cross-fork mechanics for opening a PR to a PUBLIC or not-solely-owned repo (a fork → upstream, an OSS contribution, any world-visible repo). Runs a user-configurable internal-ref scrub, a contributor checklist, and the correct cross-fork `gh pr create`. Not for your own private-workflow PRs — use `ship` for those.
---

Open a PR to a repo you do **not** solely control — a fork → upstream, an OSS
contribution, or any public repo — meeting the maintainer's bar BEFORE they bounce
it back. Skip this for your own private-workflow PRs (use `ship`).

## Step 1 — Internal-ref scrub gate (blocking)

Internal provenance (internal ticket/PR numbers, internal rule-id citations,
internal host names or absolute paths, internal product/customer names) is
world-visible in a public repo and meaningless-to-misleading in a repo that isn't
yours. A **partial** scrub is worse than none — scrub ALL of it or none.

The gate reuses goodfellow's own egress matcher against **your own** denylist —
this ships with no built-in inventory of any particular project's internal names.
Define your denylist once (first that exists wins):

1. `--denylist <path>`
2. `$GOODFELLOW_INTERNAL_DENYLIST` (a file path)
3. `<project-root>/.goodfellow/internal_denylist.txt`

One phrase per line; `#` comments; blanks ignored. List your internal-only tokens.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/public_pr_scrub.py" --base "$BASE"
# exit 0 = clean · exit 1 = hits (BLOCK) · exit 2 = --require-denylist with none set
```

If it reports hits: remove **every** internal ref from the added lines, re-run
until clean. Do NOT open the PR with any hit unresolved. (No denylist configured →
the gate passes with a note; add `--require-denylist` to hard-fail instead.)

## Step 2 — Ship only the mergeable unit

The PR should contain the **product change (src + test)**, nothing else. Park
cargo — design mocks, local plan/spec/evidence docs, deploy logs, fork-process
paper trail — on a separate branch (nothing lost; say so in the PR). The
maintainer should not have to itemize your cargo for you.

## Step 3 — Description must match shipped behavior

The PR description is a durable artifact. Every claim must be true of the code as
pushed. If you described a behavior you did not implement, build it or reword
before opening.

## Step 4 — Self-verify at the maintainer's bar

They will re-verify in a fresh clone; beat them to it. Run the full suite and
confirm the diff contains only what you claim:

```bash
# run the project's test/lint/build, then:
git diff "$BASE"...HEAD --stat
```

Surface any cross-PR dependency (a field another in-flight PR defines) and its
graceful-degradation path in the body — don't make them discover it.

## Step 5 — Cross-fork `gh pr create` mechanics

When the base repo is a fork's upstream (or otherwise not your origin), pin all
three of `--repo`, `--base`, `--head` — `gh` silently defaults `--base` to the
wrong repo otherwise:

```bash
gh pr create \
  --repo <UPSTREAM_OWNER>/<repo> \
  --base <upstream-default-branch> \
  --head <your-fork-owner>:<branch>
```

Base off the upstream default branch and expect a squash merge. Verify your push
access to the target before pushing; never force-push a branch you do not own, and
never push to a shared default branch.

For a same-owner public repo (your origin is the public repo), a normal same-repo
PR is fine — `--base <default-branch> --head <branch>` — the scrub gate (Step 1)
is the load-bearing part there.

## Step 6 — Open + record

Open with the pinned flags. In the body: what changed, why, verification results
(suite/lint/build), any cross-PR sync point, and where cargo was parked. Peer
tone. Then stop — a maintainer reviews before merge.

## Optional: wire into `ship`

`ship` targets your own repo by default and does not force this gate. When the
ship target is public / not-solely-owned, run Step 1 as a pre-open gate first:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/public_pr_scrub.py" --base "$BASE" || {
  echo "internal-ref scrub failed — do not open the public PR" >&2; exit 1; }
```
